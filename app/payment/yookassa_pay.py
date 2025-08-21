from __future__ import annotations

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse
from yookassa import Configuration, Payment

from ..config import settings
from ..db import async_session
from ..models import Order, OrderStatus, User
from ..main import bot
from sqlalchemy import select


def _ensure_config():
    if not (settings.yk_shop_id and settings.yk_api_key):
        raise RuntimeError("YooKassa credentials are not set")
    Configuration.account_id = settings.yk_shop_id
    Configuration.secret_key = settings.yk_api_key


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
                if bot and user:
                    try:
                        await bot.send_message(user.tg_user_id, f"✅ Оплата принята. Заказ #{order.id} на {order.amount}₽")
                    except Exception:
                        pass
            elif status == "canceled":
                order.status = OrderStatus.CANCELED
                await session.commit()
                if bot and user:
                    try:
                        await bot.send_message(user.tg_user_id, f"❌ Оплата отменена. Заказ #{order.id}")
                    except Exception:
                        pass
        return JSONResponse({"ok": True})

    @app.get("/payments/yookassa/success", response_class=HTMLResponse)
    async def yk_success():
        return HTMLResponse("<h1>Оплата принята</h1>")

    @app.get("/payments/yookassa/fail", response_class=HTMLResponse)
    async def yk_fail():
        return HTMLResponse("<h1>Оплата не прошла</h1>")
