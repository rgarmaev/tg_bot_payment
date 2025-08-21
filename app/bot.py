from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
import logging

from aiogram import Router, types, F
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from .config import settings
from yookassa import Payment
from yookassa.domain.exceptions import UnauthorizedError
from uuid import uuid4
from .models import User, Order, OrderStatus, Subscription
from .x3ui.client import X3UIClient


router = Router()


@dataclass
class AppDeps:
    session: AsyncSession


PLANS = [
    {"code": "m1", "title": "Месяц", "days": 30, "price": 200},
    {"code": "m3", "title": "3 Месяца", "days": 90, "price": 500},
    {"code": "m6", "title": "6 Месяцев", "days": 180, "price": 800},
    {"code": "y1", "title": "1 год", "days": 365, "price": 1500},
]


def get_plan_by_code(code: str) -> Optional[dict]:
    for p in PLANS:
        if p["code"] == code:
            return p
    return None


async def ensure_user(session: AsyncSession, tg_user_id: int) -> User:
    result = await session.execute(select(User).where(User.tg_user_id == tg_user_id))
    user = result.scalar_one_or_none()
    if user:
        return user
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert
    # Insert or ignore to avoid UNIQUE violation on races
    await session.execute(
        sqlite_insert(User).prefix_with("OR IGNORE").values(tg_user_id=tg_user_id)
    )
    result = await session.execute(select(User).where(User.tg_user_id == tg_user_id))
    return result.scalar_one()


@router.message(Command("start"))
async def cmd_start(message: types.Message, session: AsyncSession):
    async with session.begin():
        await ensure_user(session, message.from_user.id)

    kb = InlineKeyboardBuilder()
    kb.button(text="📦 Выбрать тариф", callback_data="menu:plans")
    kb.button(text="📄 Мои подписки", callback_data="menu:subs")
    kb.button(text="📲 Скачать приложение", callback_data="menu:apps")
    kb.adjust(1)

    # Build tariffs/discounts lines
    base_month_price = next((p["price"] for p in PLANS if p["code"] == "m1"), 200)
    lines = []
    for p in PLANS:
        days = p["days"]
        months = 12 if days >= 360 else max(1, round(days / 30))
        full_price = base_month_price * months
        discount = max(0, int(round((1 - (p["price"] / full_price)) * 100)))
        lines.append(f"- {p['title']}: {p['price']}₽ (скидка {discount}% при оплате за {months} мес)")

    text = (
        "🔥 Добро пожаловать в MY VPN Server!\n"
        "Доступ в сеть без ограничений!\n\n"
        "Тарифы и скидки:\n" + "\n".join(lines) + "\n\n"
        "Выберите тариф и оплатите — доступ придёт автоматически.\n\n"
        "Команды: /buy • /check • /my"
    )
    await message.answer(text, reply_markup=kb.as_markup())


@router.message(Command("buy"))
async def cmd_buy(message: types.Message, session: AsyncSession):
    async with session.begin():
        await ensure_user(session, message.from_user.id)

    kb = InlineKeyboardBuilder()
    for p in PLANS:
        kb.button(text=f"{p['title']} — {p['price']}₽", callback_data=f"plan:{p['code']}")
    kb.adjust(1)
    await message.answer("Выберите тариф:", reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("menu:plans"))
async def cb_open_plans(callback: types.CallbackQuery):
    kb = InlineKeyboardBuilder()
    for p in PLANS:
        kb.button(text=f"{p['title']} — {p['price']}₽", callback_data=f"plan:{p['code']}")
    kb.button(text="⬅️ Назад", callback_data="menu:home")
    kb.adjust(1)
    await callback.message.edit_text("Выберите тариф:", reply_markup=kb.as_markup())
    await callback.answer()


@router.callback_query(F.data == "menu:home")
async def cb_home(callback: types.CallbackQuery):
    kb = InlineKeyboardBuilder()
    kb.button(text="📦 Выбрать тариф", callback_data="menu:plans")
    kb.button(text="📄 Мои подписки", callback_data="menu:subs")
    kb.button(text="📲 Скачать приложение", callback_data="menu:apps")
    kb.adjust(1)

    base_month_price = next((p["price"] for p in PLANS if p["code"] == "m1"), 200)
    lines = []
    for p in PLANS:
        days = p["days"]
        months = 12 if days >= 360 else max(1, round(days / 30))
        full_price = base_month_price * months
        discount = max(0, int(round((1 - (p["price"] / full_price)) * 100)))
        lines.append(f"- {p['title']}: {p['price']}₽ (скидка {discount}% при оплате за {months} мес)")

    text = (
        "🔥 Добро пожаловать в MY VPN Server!\n"
        "Доступ в сеть без ограничений!\n\n"
        "Тарифы и скидки:\n" + "\n".join(lines) + "\n\n"
        "Выберите тариф и оплатите — доступ придёт автоматически.\n\n"
        "Команды: /buy • /check • /my"
    )
    await callback.message.edit_text(text, reply_markup=kb.as_markup())
    await callback.answer()


@router.callback_query(F.data == "menu:apps")
async def cb_open_apps(callback: types.CallbackQuery):
    kb = InlineKeyboardBuilder()
    kb.button(text="iOS (App Store)", url="https://apps.apple.com/app/id6476628951")
    kb.button(text="Android (Google Play)", url="https://play.google.com/store/apps/details?id=com.v2raytun.android&pcampaignid=web_share")
    kb.button(text="⬅️ Назад", callback_data="menu:home")
    kb.adjust(1)
    await callback.message.edit_text("Скачайте приложение для подключения:", reply_markup=kb.as_markup())
    await callback.answer()


@router.callback_query(F.data.startswith("menu:subs"))
async def cb_open_subs(callback: types.CallbackQuery, session: AsyncSession):
    result = await session.execute(
        select(Subscription)
        .join(User)
        .where(User.tg_user_id == callback.from_user.id)
        .order_by(Subscription.id.desc())
    )
    subs = result.scalars().all()
    if not subs:
        await callback.message.edit_text("Подписок нет. Используйте /buy.")
    else:
        lines = ["Ваши подписки:"]
        for s in subs:
            line = f"#{s.id} UUID={s.xray_uuid} active={s.is_active}"
            if s.config_url:
                line += f"\n{s.config_url}"
            lines.append(line)
        kb = InlineKeyboardBuilder()
        kb.button(text="⬅️ Назад", callback_data="menu:home")
        kb.adjust(1)
        await callback.message.edit_text("\n".join(lines), reply_markup=kb.as_markup())
    await callback.answer()


@router.callback_query(F.data.startswith("plan:"))
async def cb_plan_choose(callback: types.CallbackQuery, session: AsyncSession):
    code = callback.data.split(":", 1)[1]
    plan = get_plan_by_code(code)
    if not plan:
        await callback.answer("Тариф не найден", show_alert=True)
        return

    # Ensure user and create order within a transaction
    async with session.begin():
        user = await ensure_user(session, callback.from_user.id)

        order = Order(
            user_id=user.id,
            amount=plan["price"],
            currency="RUB",
            status=OrderStatus.PENDING,
            external_id=plan["code"],
        )
        session.add(order)
        await session.flush()

        order_id = order.id
        amount = float(order.amount)

    pay_url = None
    if settings.payment_provider == "yookassa":
        from .config import settings as app_settings
        from yookassa import Configuration
        # Проверяем, что ключи заданы
        if not (app_settings.yk_shop_id and app_settings.yk_api_key):
            await callback.answer(
                "Платёж временно недоступен: не настроены YK_SHOP_ID/YK_API_KEY.",
                show_alert=True,
            )
            return
        Configuration.account_id = app_settings.yk_shop_id
        Configuration.secret_key = app_settings.yk_api_key
        description = f"Оплата тарифа {plan['title']} (заказ #{order_id})"
        success_url = (
            app_settings.public_base_url.rstrip("/") + "/payments/yookassa/success"
        ) if app_settings.public_base_url else None
        try:
            idempotence_key = f"order-{order_id}-{uuid4()}"
            payment = Payment.create({
                "amount": {"value": f"{amount:.2f}", "currency": "RUB"},
                "confirmation": {
                    "type": "redirect",
                    **({"return_url": success_url} if success_url else {}),
                },
                "capture": True,
                "description": description,
                "metadata": {"order_id": str(order_id)},
            }, idempotence_key)
            pay_url = getattr(getattr(payment, "confirmation", None), "confirmation_url", None)
        except UnauthorizedError as e:
            logging.exception("YooKassa Unauthorized while creating payment for order %s", order_id)
            await callback.answer(
                "Ошибка авторизации платёжного шлюза. Проверьте YK_SHOP_ID/YK_API_KEY в .env.",
                show_alert=True,
            )
            return
        except Exception:
            logging.exception("Failed to create YooKassa payment for order %s", order_id)
            await callback.answer(
                "Не удалось создать платёж. Повторите позже.",
                show_alert=True,
            )
            return
    else:
        # fallback (should not be used once Robokassa fully removed)
        pay_url = None

    if not pay_url:
        await callback.answer("Не удалось сформировать ссылку оплаты", show_alert=True)
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="Оплатить", url=pay_url)
    kb.button(text="⬅️ Назад к тарифам", callback_data="menu:plans")
    kb.adjust(1)
    await callback.message.edit_text(
        f"Счёт #{order_id} на {amount:.2f}₽ создан. Оплатите по ссылке ниже.",
        reply_markup=kb.as_markup(),
    )
    await callback.answer()


@router.message(Command("check"))
async def cmd_check(message: types.Message, session: AsyncSession):
    result = await session.execute(
        select(Order)
        .join(User)
        .where(User.tg_user_id == message.from_user.id)
        .order_by(Order.id.desc())
    )
    order = result.scalars().first()
    if not order:
        await message.answer("Счетов не найдено. Используйте /buy.")
        return
    if order.status != OrderStatus.PAID:
        await message.answer(
            f"Статус счета #{order.id}: {order.status}. Подождите и повторите."
        )
        return

    # determine plan days from order code if available
    plan_days = settings.plan_days
    if order.external_id:
        p = get_plan_by_code(order.external_id)
        if p:
            plan_days = p["days"]

    async with X3UIClient(
        settings.x3ui_base_url,
        settings.x3ui_username,
        settings.x3ui_password,
    ) as x3:
        created = await x3.add_client(
            inbound_id=settings.x3ui_inbound_id,
            days=plan_days,
            traffic_gb=settings.x3ui_client_traffic_gb,
            email_note=f"tg_{message.from_user.id}",
        )

    result_user = await session.execute(
        select(User).where(User.tg_user_id == message.from_user.id)
    )
    user = result_user.scalar_one()
    sub = Subscription(
        user_id=user.id,
        inbound_id=settings.x3ui_inbound_id,
        xray_uuid=created.uuid,
        expires_at=None,
        config_url=created.config_url,
        is_active=True,
    )
    session.add(sub)
    await session.commit()

    text = (
        "Оплата подтверждена и подписка создана.\n"
        f"UUID: {created.uuid}\n"
    )
    if created.config_url:
        text += f"Ссылка конфигурации: {created.config_url}"
    else:
        text += "Получите ссылку в панели."
    await message.answer(text)


@router.message(Command("my"))
async def cmd_my(message: types.Message, session: AsyncSession):
    result = await session.execute(
        select(Subscription)
        .join(User)
        .where(User.tg_user_id == message.from_user.id)
        .order_by(Subscription.id.desc())
    )
    subs = result.scalars().all()
    if not subs:
        await message.answer("Подписок нет. Используйте /buy.")
        return
    lines = ["Ваши подписки:"]
    for s in subs:
        line = f"#{s.id} UUID={s.xray_uuid} active={s.is_active}"
        if s.config_url:
            line += f"\n{s.config_url}"
        lines.append(line)
    await message.answer("\n".join(lines))