from __future__ import annotations

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse
from yookassa import Configuration, Payment

from ..config import settings
from ..db import async_session
from ..models import Order, OrderStatus, User, Subscription
from ..x3ui.client import X3UIClient
from sqlalchemy import select
from datetime import datetime, timedelta
from urllib.parse import urlsplit, urlunsplit


def _ensure_config():
    if not (settings.yk_shop_id and settings.yk_api_key):
        raise RuntimeError("YooKassa credentials are not set")
    Configuration.account_id = settings.yk_shop_id
    Configuration.secret_key = settings.yk_api_key


def _origin_from_base(base_url: str | None) -> str | None:
    if not base_url:
        return None
    try:
        p = urlsplit(base_url)
        if not p.scheme or not p.netloc:
            return None
        return urlunsplit((p.scheme, p.netloc, "", "", ""))
    except Exception:
        return None


def register_routes(app: FastAPI) -> None:
    @app.post("/payments/yookassa/callback")
    async def yk_callback(request: Request):
        _ensure_config()
        event = await request.json()
        obj = event.get("object", {})
        payment_id = obj.get("id")
        # Перепроверяем платёж у YooKassa (рекомендация quick-start)
        try:
            remote = Payment.find_one(payment_id)
        except Exception:
            raise HTTPException(status_code=400, detail="invalid payment id")
        metadata = getattr(remote, "metadata", {}) or {}
        inv_id = metadata.get("order_id")
        if not inv_id:
            raise HTTPException(status_code=400, detail="no order id")
        status = getattr(remote, "status", None)
        async with async_session() as session:
            result = await session.execute(select(Order).where(Order.id == int(inv_id)))
            order: Order | None = result.scalar_one_or_none()
            if not order:
                raise HTTPException(status_code=404, detail="order not found")
            # Загрузим пользователя для уведомления
            user_result = await session.execute(select(User).where(User.id == order.user_id))
            user: User | None = user_result.scalar_one_or_none()
            if status == "succeeded":
                order.status = OrderStatus.PAID
                await session.commit()
                bot = getattr(request.app.state, "bot", None)
                if bot and user:
                    try:
                        await bot.send_message(user.tg_user_id, f"✅ Оплата принята. Заказ #{order.id} на {order.amount}₽")
                    except Exception:
                        pass
            elif status == "canceled":
                order.status = OrderStatus.CANCELED
                await session.commit()
                bot = getattr(request.app.state, "bot", None)
                if bot and user:
                    try:
                        await bot.send_message(user.tg_user_id, f"❌ Оплата отменена. Заказ #{order.id}")
                    except Exception:
                        pass
        return JSONResponse({"ok": True})

    @app.get("/payments/yookassa/success", response_class=HTMLResponse)
    async def yk_success(request: Request, order_id: int | None = None):
        # Мгновенная активация после возврата из YooKassa
        _ensure_config()
        if not order_id:
            return HTMLResponse("<h1>Оплата принята</h1><p>Номер заказа не передан. Используйте /check в боте.</p>")
        async with async_session() as session:
            result = await session.execute(select(Order).where(Order.id == order_id))
            order: Order | None = result.scalar_one_or_none()
            if not order:
                return HTMLResponse("<h1>Оплата принята</h1><p>Заказ не найден. Используйте /check в боте.</p>")
            # Получим payment_id из external_id вида code|payment_id
            payment_id = None
            if order.external_id and "|" in order.external_id:
                try:
                    payment_id = order.external_id.split("|", 1)[1]
                except Exception:
                    payment_id = None
            status_ok = False
            if payment_id:
                try:
                    remote = Payment.find_one(payment_id)
                    status_ok = getattr(remote, "status", None) == "succeeded"
                except Exception:
                    status_ok = False
            # Если статус пройден — отмечаем заказ как оплачен и создаём подписку
            if status_ok:
                if order.status != OrderStatus.PAID:
                    order.status = OrderStatus.PAID
                    await session.commit()
                # Получим пользователя
                user_result = await session.execute(select(User).where(User.id == order.user_id))
                user = user_result.scalar_one_or_none()
                # Определим срок плана из кода
                plan_days_map = {"m1": 30, "m3": 90, "m6": 180, "y1": 365}
                plan_code = order.external_id.split("|", 1)[0] if order.external_id else None
                plan_days = plan_days_map.get(plan_code, settings.plan_days)
                expires_at = datetime.utcnow() + timedelta(days=plan_days)
                # Создадим клиента в x3-ui и сохраним подписку
                async with X3UIClient(
                    settings.x3ui_base_url,
                    settings.x3ui_username,
                    settings.x3ui_password,
                ) as x3:
                    created = await x3.add_client(
                        inbound_id=settings.x3ui_inbound_id,
                        days=plan_days,
                        traffic_gb=settings.x3ui_client_traffic_gb,
                        email_note=f"tg_{user.tg_user_id if user else 'unknown'}_{int(datetime.utcnow().timestamp())}",
                    )
                    # Попробуем собрать ссылку конфигурации из inbound
                    cfg_url = None
                    try:
                        inbound = await x3.get_inbound(settings.x3ui_inbound_id)
                        if inbound:
                            cfg_url = x3.build_vless_url(inbound, created.uuid, f"tg_{user.tg_user_id}")
                    except Exception:
                        cfg_url = None
                # Попробуем собрать ссылку подписки по email (note)
                sub_url = None
                origin = _origin_from_base(settings.public_base_url)
                if origin and settings.x3ui_subscription_port and settings.x3ui_subscription_path:
                    p = settings.x3ui_subscription_path
                    if not p.startswith("/"):
                        p = "/" + p
                    if not p.endswith("/"):
                        p = p + "/"
                    sub_token = created.note or f"tg_{user.tg_user_id if user else 'unknown'}"
                    host = origin.split('://')[1].split('/')[0].split(':')[0]
                    sub_url = f"{origin.split('://')[0]}://{host}:{settings.x3ui_subscription_port}{p}{sub_token}"
                sub = Subscription(
                    user_id=order.user_id,
                    inbound_id=settings.x3ui_inbound_id,
                    xray_uuid=created.uuid,
                    expires_at=expires_at,
                    config_url=cfg_url or sub_url or created.config_url,
                    is_active=True,
                )
                session.add(sub)
                await session.commit()
                # Уведомим пользователя в Telegram
                bot = getattr(request.app.state, "bot", None)
                if bot and user:
                    try:
                        text = "Оплата подтверждена и подписка создана.\n" f"UUID: {created.uuid}\n"
                        if cfg_url or sub_url or created.config_url:
                            text += f"Ссылка конфигурации: {cfg_url or sub_url or created.config_url}"
                        await bot.send_message(user.tg_user_id, text)
                    except Exception:
                        pass
                return HTMLResponse("<h1>Оплата подтверждена</h1><p>Подписка активирована. Проверьте сообщения в Telegram.</p>")
            # Иначе сообщим ожидание
            return HTMLResponse("<h1>Оплата в обработке</h1><p>Мы ещё не получили подтверждение от YooKassa. Ожидайте или используйте /check в боте.</p>")

    @app.get("/payments/yookassa/fail", response_class=HTMLResponse)
    async def yk_fail():
        return HTMLResponse("<h1>Оплата не прошла</h1>")
