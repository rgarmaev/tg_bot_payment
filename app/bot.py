from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from aiogram import Router, types, F
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from .config import settings
from .payment.robokassa import build_payment_url
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


@router.message(Command("start"))
async def cmd_start(message: types.Message, session: AsyncSession):
    # Idempotent user create (handles concurrent updates)
    async with session.begin():
        result = await session.execute(select(User).where(User.tg_user_id == message.from_user.id))
        user = result.scalar_one_or_none()
        if not user:
            try:
                user = User(tg_user_id=message.from_user.id)
                session.add(user)
            except Exception:
                await session.rollback()
                result = await session.execute(select(User).where(User.tg_user_id == message.from_user.id))
                user = result.scalar_one()

    kb = InlineKeyboardBuilder()
    kb.button(text="📦 Выбрать тариф", callback_data="menu:plans")
    kb.button(text="📄 Мои подписки", callback_data="menu:subs")
    kb.adjust(1)

    text = (
        "🔥 Добро пожаловать в VPN бот!\n\n"
        "Выберите тариф и оплатите через Robokassa — доступ придёт автоматически.\n\n"
        "Команды: /buy • /check • /my"
    )
    await message.answer(text, reply_markup=kb.as_markup())


@router.message(Command("buy"))
async def cmd_buy(message: types.Message, session: AsyncSession):
    async with session.begin():
        result = await session.execute(select(User).where(User.tg_user_id == message.from_user.id))
        user = result.scalar_one_or_none()
        if not user:
            try:
                user = User(tg_user_id=message.from_user.id)
                session.add(user)
            except Exception:
                await session.rollback()
                result = await session.execute(select(User).where(User.tg_user_id == message.from_user.id))
                user = result.scalar_one()

    kb = InlineKeyboardBuilder()
    for p in PLANS:
        kb.button(text=f"{p[title]} — {p[price]}₽", callback_data=f"plan:{p[code]}")
    kb.adjust(1)
    await message.answer("Выберите тариф:", reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("menu:plans"))
async def cb_open_plans(callback: types.CallbackQuery):
    kb = InlineKeyboardBuilder()
    for p in PLANS:
        kb.button(text=f"{p[title]} — {p[price]}₽", callback_data=f"plan:{p[code]}")
    kb.adjust(1)
    await callback.message.edit_text("Выберите тариф:", reply_markup=kb.as_markup())
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
        await callback.message.edit_text("\n".join(lines))
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
        result = await session.execute(select(User).where(User.tg_user_id == callback.from_user.id))
        user = result.scalar_one_or_none()
        if not user:
            user = User(tg_user_id=callback.from_user.id)
            session.add(user)
            await session.flush()

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

    try:
        pay_url = build_payment_url(order_id, amount, f"Оплата тарифа {plan[title]}")
    except Exception as e:
        await callback.answer(f"Ошибка Robokassa: {e}", show_alert=True)
        return

    kb = InlineKeyboardBuilder()
    kb.button(text="Оплатить через Robokassa", url=pay_url)
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
