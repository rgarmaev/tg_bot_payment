from __future__ import annotations

import hashlib
from urllib.parse import urlencode

from fastapi import FastAPI, Request, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from ..config import settings
from ..db import async_session
from ..models import Order, OrderStatus


def _gateway_url() -> str:
    return (settings.robokassa_gateway_url or "https://auth.robokassa.ru/Merchant/Index.aspx").rstrip("/")


def _signature_md5(*parts: str) -> str:
    src = ":".join(parts)
    return hashlib.md5(src.encode("utf-8")).hexdigest()


def build_payment_url(order_id: int, amount: int, description: str = "Оплата подписки") -> str:
    if not (settings.robokassa_login and settings.robokassa_password1):
        raise RuntimeError("Robokassa credentials are not set")
    out_sum = f"{amount:.2f}".replace(",", ".")
    mrh_login = settings.robokassa_login
    inv_id = str(order_id)
    is_test = "1" if settings.robokassa_is_test else "0"
    signature = _signature_md5(mrh_login, out_sum, inv_id, settings.robokassa_password1)
    params = {
        "MerchantLogin": mrh_login,
        "OutSum": out_sum,
        "InvId": inv_id,
        "Description": description,
        "SignatureValue": signature,
        "IsTest": is_test,
        "Culture": settings.robokassa_culture,
    }
    # add return URLs if public base available
    if settings.public_base_url:
        base = settings.public_base_url.rstrip("/")
        params.update(
            {
                "SuccessURL": f"{base}/payments/robokassa/success",
                "FailURL": f"{base}/payments/robokassa/fail",
            }
        )
    return f"{_gateway_url()}?{urlencode(params)}"


def register_routes(app: FastAPI) -> None:
    @app.post("/payments/robokassa/result", response_class=PlainTextResponse)
    async def robokassa_result(request: Request):
        form = dict(await request.form())
        out_sum = form.get("OutSum")
        inv_id = form.get("InvId")
        sig = (form.get("SignatureValue") or "").lower()
        expected = _signature_md5(out_sum or "", inv_id or "", settings.robokassa_password2 or "")
        if sig != expected:
            raise HTTPException(status_code=400, detail="invalid signature")
        async with async_session() as session:
            result = await session.execute(select(Order).where(Order.id == int(inv_id)))
            order: Order | None = result.scalar_one_or_none()
            if not order:
                raise HTTPException(status_code=404, detail="order not found")
            order.status = OrderStatus.PAID
            await session.commit()
        return PlainTextResponse(f"OK{inv_id}")

    @app.get("/payments/robokassa/success", response_class=HTMLResponse)
    async def robokassa_success(InvId: int):  # noqa: N803
        return HTMLResponse(f"<h1>Оплата принята. Номер счёта: {InvId}</h1>")

    @app.get("/payments/robokassa/fail", response_class=HTMLResponse)
    async def robokassa_fail(InvId: int):  # noqa: N803
        return HTMLResponse(f"<h1>Оплата не прошла. Номер счёта: {InvId}</h1>")
