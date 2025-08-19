from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from ..db import async_session
from ..models import Order, OrderStatus


def register_routes(app: FastAPI) -> None:
    @app.get("/pay/mock/{order_id}", response_class=HTMLResponse)
    async def mock_pay(order_id: int):
        async with async_session() as session:
            result = await session.execute(select(Order).where(Order.id == order_id))
            order: Order | None = result.scalar_one_or_none()
            if not order:
                return HTMLResponse("<h1>Order not found</h1>", status_code=404)
            order.status = OrderStatus.PAID
            await session.commit()
            return HTMLResponse(
                f"""
                <html><body>
                <h1>Order #{order.id} marked as PAID</h1>
                <p>You can return to Telegram and run /check to proceed.</p>
                </body></html>
                """
            )
