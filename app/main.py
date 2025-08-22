from __future__ import annotations

import asyncio
import contextlib
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from fastapi.responses import PlainTextResponse
from aiogram import Bot, Dispatcher, BaseMiddleware
from aiogram.types import Update

from .config import settings
from .db import init_db, async_session
from .bot import router as bot_router
from .payment.yookassa_pay import register_routes as register_yookassa_routes


bot: Bot | None = None
dispatcher: Dispatcher | None = None


class SessionMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: Update, data):
        if "session" in data:
            return await handler(event, data)
        async with async_session() as s:
            data["session"] = s
            return await handler(event, data)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global bot, dispatcher
    await init_db()

    polling_task = None
    if settings.telegram_bot_token and settings.telegram_bot_token != "CHANGE_ME":
        bot = Bot(token=settings.telegram_bot_token)
        dispatcher = Dispatcher()
        dispatcher.message.middleware(SessionMiddleware())
        dispatcher.callback_query.middleware(SessionMiddleware())
        dispatcher.include_router(bot_router)
        # expose bot for routes
        app.state.bot = bot
        polling_task = asyncio.create_task(dispatcher.start_polling(bot))
    try:
        yield
    finally:
        if polling_task:
            polling_task.cancel()
            with contextlib.suppress(Exception):
                await polling_task
        if bot:
            await bot.session.close()


app = FastAPI(lifespan=lifespan)


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/", include_in_schema=False, response_class=PlainTextResponse)
async def root():
    return "OK"


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(status_code=204)


if (settings.payment_provider or "").lower() == "yookassa":
	register_yookassa_routes(app)
