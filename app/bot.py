from __future__ import annotations

from dataclasses import dataclass

from aiogram import Router, types
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


@router.message(Command("start"))
async def cmd_start(message: types.Message, session: AsyncSession):
    result = await session.execute(
        select(User).where(User.tg_user_id == message.from_user.id)
    )
    user = result.scalar_one_or_none()
    if not user:
        user = User(tg_user_id=message.from_user.id)
        session.add(user)
        await session.commit()
    await message.answer(
        "Добро пожаловать!\n"
        f"Тариф: {settings.plan_name} — {settings.plan_days} дней, {settings.plan_price_rub}₽\n"
        "Команды: /buy — купить, /check — проверить оплату, /my — мои подписки"
    )


@router.message(Command("buy"))
async def cmd_buy(message: types.Message, session: AsyncSession):
    result = await session.execute(
        select(User).where(User.tg_user_id == message.from_user.id)
    )
    user = result.scalar_one_or_none()
    if not user:
        user = User(tg_user_id=message.from_user.id)
        session.add(user)
        await session.commit()

    order = Order(
        user_id=user.id,
        amount=settings.plan_price_rub,
        currency="RUB",
        status=OrderStatus.PENDING,
    )
    session.add(order)
    await session.commit()

    # Robokassa payment link
    description = f"Оплата счёта #{order.id}"
    try:
        pay_url = build_payment_url(order.id, order.amount, description)
    except Exception as e:
        await message.answer(f"Ошибка формирования ссылки Robokassa: {e}")
        return

    kb = InlineKeyboardBuilder()
    kb.button(text="Оплатить через Robokassa", url=pay_url)
    kb.adjust(1)

    await message.answer(
        f"Счёт #{order.id} на {order.amount}₽ создан. Оплатите по ссылке ниже.",
        reply_markup=kb.as_markup(),
    )


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

    async with X3UIClient(
        settings.x3ui_base_url,
        settings.x3ui_username,
        settings.x3ui_password,
    ) as x3:
        created = await x3.add_client(
            inbound_id=settings.x3ui_inbound_id,
            days=settings.plan_days,
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
