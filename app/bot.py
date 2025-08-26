from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
import logging
import asyncio
from uuid import uuid4
from urllib.parse import urlsplit, urlunsplit
from datetime import datetime, timedelta

from aiogram import Router, types, F
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from .config import settings
from yookassa import Payment
from yookassa.domain.exceptions import UnauthorizedError
from .models import User, Order, OrderStatus, Subscription
from .x3ui.client import X3UIClient
from .db import async_session
import base64
import re
import httpx
from .utils import sanitize_config_link


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


def _origin_from_base_url(base_url: Optional[str]) -> Optional[str]:
    if not base_url:
        return None
    try:
        parts = urlsplit(base_url)
        if not parts.scheme or not parts.netloc:
            return None
        return urlunsplit((parts.scheme, parts.netloc, "", "", ""))
    except Exception:
        return None


async def _resolve_subscription_link(url: Optional[str]) -> Optional[str]:
    """Fetch subscription endpoint and extract a single config URL (vless/vmess/trojan).

    - Accepts plain body with link
    - Accepts base64-encoded body with a single link
    - Accepts HTML with data:image/png;base64 (QR)
    - Accepts binary PNG and decodes QR (if dependencies available)
    - Returns first protocol link found or None
    """
    if not url:
        return None
    try:
        from .config import settings as app_settings
        log = logging.getLogger("x3ui")
        async with httpx.AsyncClient(timeout=10, follow_redirects=True, verify=app_settings.x3ui_verify_tls) as client:
            for attempt in range(5):
                resp = await client.get(url)
                ctype = resp.headers.get("content-type", "").lower()
                preview = None
                try:
                    preview = (resp.text or "").strip()[:160]
                except Exception:
                    preview = "<binary>"
                log.info("Stage:sub_resolve attempt=%s url=%s -> %s ctype=%s preview=%s", attempt + 1, url, resp.status_code, ctype, preview)
                if resp.status_code != 200:
                    await asyncio.sleep(0.5)
                    continue
                text = None
                try:
                    text = resp.text.strip()
                except Exception:
                    text = None
                # Try to find direct link in plain text
                if text:
                    m = re.search(r"(vless://[^\s\"'<]+|vmess://[^\s\"'<]+|trojan://[^\s\"'<]+)", text)
                    if m:
                        return m.group(1)
                # Try base64 decode then search
                try:
                    if text:
                        decoded = base64.b64decode(text + "==").decode("utf-8", errors="ignore")
                        m2 = re.search(r"(vless://[^\s\"'<]+|vmess://[^\s\"'<]+|trojan://[^\s\"'<]+)", decoded)
                        if m2:
                            return m2.group(1)
                except Exception:
                    pass
                # Extract data URI QR from HTML
                if text:
                    try:
                        mimg = re.search(r"data:image/png;base64,([A-Za-z0-9+/=]+)", text)
                        if mimg:
                            raw = base64.b64decode(mimg.group(1))
                            try:
                                from PIL import Image  # type: ignore
                                from io import BytesIO
                                try:
                                    from pyzbar.pyzbar import decode as qr_decode  # type: ignore
                                except Exception:
                                    qr_decode = None
                                if qr_decode is not None:
                                    img = Image.open(BytesIO(raw))
                                    dec = qr_decode(img)
                                    for d in dec:
                                        data = d.data.decode("utf-8", errors="ignore")
                                        if data.startswith(("vless://", "vmess://", "trojan://")):
                                            return data
                            except Exception:
                                pass
                    except Exception:
                        pass
                # If response is PNG image, try QR decode
                if "image/png" in ctype:
                    try:
                        raw = resp.content
                        from PIL import Image  # type: ignore
                        from io import BytesIO
                        try:
                            from pyzbar.pyzbar import decode as qr_decode  # type: ignore
                        except Exception:
                            qr_decode = None
                        if qr_decode is not None:
                            img = Image.open(BytesIO(raw))
                            dec = qr_decode(img)
                            for d in dec:
                                data = d.data.decode("utf-8", errors="ignore")
                                if data.startswith(("vless://", "vmess://", "trojan://")):
                                    return data
                    except Exception:
                        pass
                # If nothing extracted, wait a bit and retry (e.g., body == 'requesting')
                await asyncio.sleep(0.6)
    except Exception:
        pass
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

    # Deep-link: /start paid_{order_id} -> сразу проверяем оплату и активируем
    try:
        if message.text and " " in message.text:
            payload = message.text.split(" ", 1)[1].strip()
            if payload.startswith("paid_"):
                try:
                    order_id = int(payload.split("_", 1)[1])
                except Exception:
                    order_id = None
                if order_id:
                    await message.answer("Проверяю оплату...")
                    # Попробуем обновить статус из YooKassa
                    upd = await _try_refresh_order_status(order_id)
                    # Если не успел обновиться, просто упадём в обычный /check
                    if upd != OrderStatus.PAID:
                        await cmd_check(message, session)
                        return
                    # Создадим подписку как в /check
                    plan_days = settings.plan_days
                    result_order = await session.execute(select(Order).where(Order.id == order_id))
                    order = result_order.scalar_one_or_none()
                    if order and order.external_id:
                        plan_code = order.external_id.split("|", 1)[0] if "|" in order.external_id else order.external_id
                        p = get_plan_by_code(plan_code)
                        if p:
                            plan_days = p["days"]
                    expires_at = datetime.utcnow() + timedelta(days=plan_days)
                    async with X3UIClient(
                        settings.x3ui_base_url,
                        settings.x3ui_username,
                        settings.x3ui_password,
                    ) as x3:
                        # Idempotency guard: skip if recent subscription exists (10 min)
                        recent_since = datetime.utcnow() - timedelta(minutes=10)
                        existing_sub = await session.execute(
                            select(Subscription)
                            .join(User)
                            .where(
                                User.tg_user_id == message.from_user.id,
                                Subscription.created_at >= recent_since,
                                Subscription.is_active == True,
                            )
                            .order_by(Subscription.id.desc())
                        )
                        if existing_sub.scalars().first():
                            await message.answer("Подписка уже активирована недавно. Проверьте /my.")
                            return
                        created = await x3.add_client(
                            inbound_id=settings.x3ui_inbound_id,
                            days=plan_days,
                            traffic_gb=settings.x3ui_client_traffic_gb,
                            email_note=f"tg_{message.from_user.id}_{int(datetime.utcnow().timestamp())}",
                        )
                        # Не формируем локально ссылку
                    # subscription URL
                    sub_url = None
                    origin = _origin_from_base_url(settings.public_base_url)
                    if origin and settings.x3ui_subscription_port and settings.x3ui_subscription_path:
                        pth = settings.x3ui_subscription_path
                        if not pth.startswith("/"):
                            pth = "/" + pth
                        if not pth.endswith("/"):
                            pth = pth + "/"
                        sub_token = created.note or f"tg_{message.from_user.id}"
                        sub_url = f"{origin.split('://')[0]}://{origin.split('://')[1].split('/')[0].split(':')[0]}:{settings.x3ui_subscription_port}{pth}{sub_token}"
                    # If server didn't return link but sub_url is available, try resolving it
                    final_url = created.config_url or await _resolve_subscription_link(sub_url)
                    # Сохраним подписку
                    result_user = await session.execute(select(User).where(User.tg_user_id == message.from_user.id))
                    user = result_user.scalar_one()
                    sub = Subscription(
                        user_id=user.id,
                        inbound_id=settings.x3ui_inbound_id,
                        xray_uuid=created.uuid,
                        expires_at=expires_at,
                        config_url=final_url or sub_url,
                        is_active=True,
                    )
                    session.add(sub)
                    await session.commit()

                    text = (
                        "Оплата подтверждена и подписка создана.\n"
                        f"UUID: {created.uuid}\n"
                    )
                    if final_url or sub_url:
                        safe_url = sanitize_config_link(final_url or sub_url)
                        text += f"Ссылка конфигурации: {safe_url}"
                    await message.answer(text)
                    return
    except Exception:
        # Если не удалось обработать диплинк — попробуем обычную проверку и не показываем меню
        import logging as _log
        _log.exception("Deep-link /start paid_ flow failed")
        try:
            await cmd_check(message, session)
            return
        except Exception:
            pass

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
    if settings.support_chat_url:
        kb.button(text="🛠 Техническая поддержка", url=settings.support_chat_url)
    if settings.instruction_url:
        kb.button(text="📘 Инструкция", url=settings.instruction_url)
    kb.adjust(1)
    await callback.message.edit_text("Главное меню:", reply_markup=kb.as_markup())
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
            hdr = f"#{s.id} UUID={s.xray_uuid} active={s.is_active}"
            from html import escape as _esc
            line = f"<code>{_esc(hdr)}</code>"
            if s.config_url:
                safe = sanitize_config_link(s.config_url)
                line += f"\n{_esc(safe or '')}"
            lines.append(line)
        kb = InlineKeyboardBuilder()
        kb.button(text="⬅️ Назад", callback_data="menu:home")
        kb.adjust(1)
        await callback.message.edit_text("\n".join(lines), reply_markup=kb.as_markup(), parse_mode="HTML")
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
    if (settings.payment_provider or "").lower() == "yookassa":
        from .config import settings as app_settings
        from yookassa import Configuration
        # Проверяем, что ключи заданы
        if not (app_settings.yk_shop_id and app_settings.yk_api_key):
            await callback.answer(
                "Платёж временно недоступен: не настроены YK_SHOP_ID/YK_API_KEY.",
                show_alert=True,
            )
            return
        def _clean(value: str | None) -> str:
            return (value or "").strip().strip('"').strip("'")
        Configuration.account_id = _clean(app_settings.yk_shop_id)
        Configuration.secret_key = _clean(app_settings.yk_api_key)
        try:
            masked = ("test_" if Configuration.secret_key.startswith("test_") else "live_") + "***"
        except Exception:
            masked = "***"
        logging.info(
            "Using YooKassa config: shop_id=%s, key=%s (len=%s)",
            str(Configuration.account_id), masked, len(Configuration.secret_key or ""),
        )
        description = f"Оплата тарифа {plan['title']} (заказ #{order_id})"
        # Предпочитаем возврат в Telegram через deep-link
        me = await callback.bot.get_me()
        deep_link = f"https://t.me/{me.username}?start=paid_{order_id}"
        origin = _origin_from_base_url(app_settings.public_base_url)
        success_url = deep_link if True else ((origin + f"/payments/yookassa/success?order_id={order_id}") if origin else None)
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
            confirmation = getattr(payment, "confirmation", None)
            pay_url = None
            if confirmation is not None:
                # SDK объект или dict
                pay_url = getattr(confirmation, "confirmation_url", None)
                if not pay_url and isinstance(confirmation, dict):
                    pay_url = confirmation.get("confirmation_url")
            if not pay_url:
                # Попробуем получить платёж повторно (иногда SDK возвращает объект без ссылки сразу)
                try:
                    refreshed = Payment.find_one(getattr(payment, "id", None))
                    ref_conf = getattr(refreshed, "confirmation", None)
                    pay_url = getattr(ref_conf, "confirmation_url", None)
                    if not pay_url and isinstance(ref_conf, dict):
                        pay_url = ref_conf.get("confirmation_url")
                except Exception:
                    logging.exception("Failed to refresh YooKassa payment %s", getattr(payment, "id", None))
            if not pay_url:
                logging.error("YooKassa payment has no confirmation_url after refresh: %s", getattr(payment, "id", None))
            # Сохраним ссылку и payment_id в заказ
            try:
                payment_id = getattr(payment, "id", None)
                ext_value = f"{plan['code']}|{payment_id}" if payment_id else plan['code']
                async with session.begin():
                    result = await session.execute(select(Order).where(Order.id == order_id))
                    upd_order = result.scalar_one()
                    upd_order.payment_url = pay_url
                    upd_order.external_id = ext_value
            except Exception:
                logging.exception("Failed to save payment data to order %s", order_id)
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
        await callback.answer(
            "Платёжный провайдер не настроен. Установите PAYMENT_PROVIDER=yookassa.",
            show_alert=True,
        )
        return

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

    # Автопроверка оплаты: 3 попытки каждые 3 минуты без вебхука
    try:
        asyncio.create_task(_auto_check_and_activate(callback.bot, callback.from_user.id, order_id))
    except Exception:
        logging.exception("Failed to schedule auto check for order %s", order_id)


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
        # Попробуем подтянуть статус из YooKassa без вебхука
        if (settings.payment_provider or "").lower() == "yookassa":
            payment_id: Optional[str] = None
            if order.external_id and "|" in order.external_id:
                try:
                    payment_id = order.external_id.split("|", 1)[1]
                except Exception:
                    payment_id = None
            if payment_id:
                try:
                    from yookassa import Configuration
                    from .config import settings as app_settings
                    def _clean(v: Optional[str]) -> str:
                        return (v or "").strip().strip('"').strip("'")
                    Configuration.account_id = _clean(app_settings.yk_shop_id)
                    Configuration.secret_key = _clean(app_settings.yk_api_key)
                    remote = Payment.find_one(payment_id)
                    remote_status = getattr(remote, "status", None)
                    if remote_status == "succeeded":
                        order.status = OrderStatus.PAID
                        await session.commit()
                    elif remote_status == "canceled":
                        order.status = OrderStatus.CANCELED
                        await session.commit()
                        await message.answer(f"Оплата отменена. Заказ #{order.id}")
                        return
                except Exception:
                    # Игнорируем ошибки сети/SDK и покажем текущий локальный статус
                    pass
        if order.status != OrderStatus.PAID:
            await message.answer(
                f"Статус счета #{order.id}: {order.status}. Подождите и повторите."
            )
            return

    # determine plan days from order code if available
    plan_days = settings.plan_days
    if order.external_id:
        p = get_plan_by_code(order.external_id if "|" not in order.external_id else order.external_id.split("|", 1)[0])
        if p:
            plan_days = p["days"]
    expires_at = datetime.utcnow() + timedelta(days=plan_days)

    async with X3UIClient(
        settings.x3ui_base_url,
        settings.x3ui_username,
        settings.x3ui_password,
    ) as x3:
        # Idempotency guard: skip if recent subscription exists (10 min)
        recent_since = datetime.utcnow() - timedelta(minutes=10)
        existing_sub = await session.execute(
            select(Subscription)
            .join(User)
            .where(
                User.tg_user_id == message.from_user.id,
                Subscription.created_at >= recent_since,
                Subscription.is_active == True,
            )
            .order_by(Subscription.id.desc())
        )
        if existing_sub.scalars().first():
            await message.answer("Подписка уже активирована недавно. Проверьте /my.")
            return
        created = await x3.add_client(
            inbound_id=settings.x3ui_inbound_id,
            days=plan_days,
            traffic_gb=settings.x3ui_client_traffic_gb,
            email_note=f"tg_{message.from_user.id}_{int(datetime.utcnow().timestamp())}",
        )
    # subscription URL (if configured)
    sub_url = None
    origin = _origin_from_base_url(settings.public_base_url)
    if origin and settings.x3ui_subscription_port and settings.x3ui_subscription_path:
        pth = settings.x3ui_subscription_path
        if not pth.startswith("/"):
            pth = "/" + pth
        if not pth.endswith("/"):
            pth = pth + "/"
        sub_token = created.note or f"tg_{message.from_user.id}"
        sub_url = f"{origin.split('://')[0]}://{origin.split('://')[1].split('/')[0].split(':')[0]}:{settings.x3ui_subscription_port}{pth}{sub_token}"
    final_url = created.config_url or await _resolve_subscription_link(sub_url)

    result_user = await session.execute(
        select(User).where(User.tg_user_id == message.from_user.id)
    )
    user = result_user.scalar_one()
    sub = Subscription(
        user_id=user.id,
        inbound_id=settings.x3ui_inbound_id,
        xray_uuid=created.uuid,
        expires_at=expires_at,
        config_url=final_url or sub_url,
        is_active=True,
    )
    session.add(sub)
    await session.commit()

    text = (
        "Оплата подтверждена и подписка создана.\n"
        f"UUID: {created.uuid}\n"
    )
    if final_url or sub_url:
        safe_url = sanitize_config_link(final_url or sub_url)
        text += f"Ссылка конфигурации: {safe_url}"
    else:
        text += "Не удалось сгенерировать ссылку автоматически. Получите её в панели администратора."
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
        hdr = f"#{s.id} UUID={s.xray_uuid} active={s.is_active}"
        from html import escape as _esc
        line = f"<code>{_esc(hdr)}</code>"
        if s.config_url:
            safe = sanitize_config_link(s.config_url)
            line += f"\n{_esc(safe or '')}"
        lines.append(line)
    await message.answer("\n".join(lines), parse_mode="HTML")


async def _try_refresh_order_status(order_id: int) -> Optional[str]:
    """Возвращает новый статус ('paid'/'canceled'/None), если удалось обновить."""
    from yookassa import Payment, Configuration
    from .config import settings as app_settings
    def _clean(v: Optional[str]) -> str:
        return (v or "").strip().strip('"').strip("'")
    if (settings.payment_provider or "").lower() != "yookassa":
        return None
    async with async_session() as s:
        res = await s.execute(select(Order).where(Order.id == order_id))
        order = res.scalar_one_or_none()
        if not order:
            return None
        payment_id: Optional[str] = None
        if order.external_id and "|" in order.external_id:
            try:
                payment_id = order.external_id.split("|", 1)[1]
            except Exception:
                payment_id = None
        if not payment_id:
            return None
        try:
            Configuration.account_id = _clean(app_settings.yk_shop_id)
            Configuration.secret_key = _clean(app_settings.yk_api_key)
            remote = Payment.find_one(payment_id)
            remote_status = getattr(remote, "status", None)
            if remote_status == "succeeded" and order.status != OrderStatus.PAID:
                order.status = OrderStatus.PAID
                await s.commit()
                return OrderStatus.PAID
            if remote_status == "canceled" and order.status != OrderStatus.CANCELED:
                order.status = OrderStatus.CANCELED
                await s.commit()
                return OrderStatus.CANCELED
        except Exception:
            logging.exception("Auto-check: failed to refresh order %s", order_id)
    return None


async def _auto_check_and_activate(bot: types.Bot, tg_user_id: int, order_id: int) -> None:
    """Три попытки с паузой 3 мин: если платёж прошёл — активируем подписку и уведомляем пользователя."""
    for attempt in range(3):
        try:
            # Ждём 3 минуты перед каждой попыткой (итого: 3, 6, 9 минут)
            await asyncio.sleep(180)
            new_status = await _try_refresh_order_status(order_id)
            if new_status == OrderStatus.PAID:
                # Создадим подписку как в /check
                async with async_session() as s:
                    res_user = await s.execute(select(User).where(User.tg_user_id == tg_user_id))
                    user = res_user.scalar_one_or_none()
                    res_order = await s.execute(select(Order).where(Order.id == order_id))
                    order = res_order.scalar_one_or_none()
                    if not user or not order:
                        return
                    # Определим дни по внешнему коду
                    plan_days = settings.plan_days
                    if order.external_id:
                        plan_code = order.external_id.split("|", 1)[0] if "|" in order.external_id else order.external_id
                        p = get_plan_by_code(plan_code)
                        if p:
                            plan_days = p["days"]
                    expires_at = datetime.utcnow() + timedelta(days=plan_days)
                
                async with X3UIClient(
                    settings.x3ui_base_url,
                    settings.x3ui_username,
                    settings.x3ui_password,
                ) as x3:
                    # Idempotency guard: skip if recent subscription exists (10 min)
                    recent_since = datetime.utcnow() - timedelta(minutes=10)
                    existing_sub = await s.execute(
                        select(Subscription)
                        .join(User)
                        .where(
                            User.tg_user_id == tg_user_id,
                            Subscription.created_at >= recent_since,
                            Subscription.is_active == True,
                        )
                        .order_by(Subscription.id.desc())
                    )
                    if existing_sub.scalars().first():
                        return
                    created = await x3.add_client(
                        inbound_id=settings.x3ui_inbound_id,
                        days=plan_days,
                        traffic_gb=settings.x3ui_client_traffic_gb,
                        email_note=f"tg_{tg_user_id}_{int(datetime.utcnow().timestamp())}",
                    )
                
                async with async_session() as s:
                    res_user = await s.execute(select(User).where(User.tg_user_id == tg_user_id))
                    user = res_user.scalar_one()
                    # Try resolving subscription link too in auto-activation
                    origin = _origin_from_base_url(settings.public_base_url)
                    sub_url = None
                    if origin and settings.x3ui_subscription_port and settings.x3ui_subscription_path:
                        pth = settings.x3ui_subscription_path
                        if not pth.startswith("/"):
                            pth = "/" + pth
                        if not pth.endswith("/"):
                            pth = pth + "/"
                        sub_token = created.note or f"tg_{tg_user_id}"
                        sub_url = f"{origin.split('://')[0]}://{origin.split('://')[1].split('/')[0].split(':')[0]}:{settings.x3ui_subscription_port}{pth}{sub_token}"
                    final_url = created.config_url or await _resolve_subscription_link(sub_url)
                    sub = Subscription(
                        user_id=user.id,
                        inbound_id=settings.x3ui_inbound_id,
                        xray_uuid=created.uuid,
                        expires_at=expires_at,
                        config_url=final_url or sub_url,
                        is_active=True,
                    )
                    s.add(sub)
                    await s.commit()
                text = "Оплата подтверждена и подписка создана.\n" f"UUID: {created.uuid}\n"
                if final_url or sub_url:
                    safe_url = sanitize_config_link(final_url or sub_url)
                    text += f"Ссылка конфигурации: {safe_url}"
                else:
                    text += "Получите ссылку в панели."
                await bot.send_message(tg_user_id, text)
                return
            elif new_status == OrderStatus.CANCELED:
                await bot.send_message(tg_user_id, f"Оплата отменена. Заказ #{order_id}")
                return
        except Exception:
            logging.exception("Auto-check attempt %s failed for order %s", attempt + 1, order_id)
        # Переходим к следующей попытке (если ещё есть)