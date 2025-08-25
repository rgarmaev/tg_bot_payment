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
from .x3ui.client import X3UIClient
import httpx


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


@app.get("/debug/x3ui/ping")
async def debug_x3ui_ping():
    """Диагностика подключения к X3-UI и чтения inbound.

    Возвращает JSON с:
    - достижимостью base_url
    - пробами login-эндпоинта
    - результатом get_inbound(settings.x3ui_inbound_id)
    """
    base_url = settings.x3ui_base_url
    inbound_id = settings.x3ui_inbound_id

    result: dict = {
        "ok": False,
        "base_url": base_url,
        "inbound_id": inbound_id,
        "probes": {},
        "hints": [],
    }

    # 1) Базовая достижимость
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(base_url, follow_redirects=True)
            result["probes"]["base_get"] = {
                "status": r.status_code,
                "server": r.headers.get("server"),
                "location": r.headers.get("location"),
            }
    except Exception as e:
        result["probes"]["base_get_error"] = str(e)
        result["hints"].append("BASE_URL недостижим из контейнера/процесса. Проверьте DNS/файрвол/порт.")

    # 2) Пробы login-урлами (без изменения состояния)
    login_paths = ["/login", "/x3ui/login", "/panel/login"]
    try:
        async with httpx.AsyncClient(base_url=base_url, timeout=10) as client:
            for p in login_paths:
                try:
                    rr = await client.get(p, follow_redirects=True)
                    result["probes"][f"GET {p}"] = rr.status_code
                except Exception as ee:
                    result["probes"][f"GET {p} error"] = str(ee)
    except Exception:
        pass

    # 3) Попытка логина и чтения inbound через X3UIClient
    try:
        async with X3UIClient(settings.x3ui_base_url, settings.x3ui_username, settings.x3ui_password, verify_tls=settings.x3ui_verify_tls) as x3:
            try:
                await x3.login()
                result["probes"]["login_attempt"] = "done"
            except Exception as e:
                result["probes"]["login_error"] = str(e)
                result["hints"].append("Не удалось выполнить login(). Проверьте X3UI_USERNAME/X3UI_PASSWORD.")
            inbound = await x3.get_inbound(inbound_id)
            if inbound:
                # Не возвращаем целиком; только основные поля
                result["inbound"] = {k: inbound.get(k) for k in ("id", "port", "protocol", "streamSettings") if k in inbound}
                result["ok"] = True
            else:
                result["inbound"] = None
                result["hints"].append("Не удалось получить inbound. Проверьте inbound_id и права/совместимость API.")
    except Exception as e:
        result["probes"]["x3ui_error"] = str(e)
        result["hints"].append("Ошибка при обращении к X3-UI. Проверьте X3UI_BASE_URL и что это не адрес FastAPI.")

    # Частые подсказки
    if base_url.startswith("http://127.0.0.1") or base_url.startswith("http://localhost"):
        result["hints"].append("127.0.0.1/localhost внутри контейнера указывает на сам контейнер, а не хост. Используйте IP хоста или имя сервиса в сети Docker.")
    if not settings.x3ui_username or not settings.x3ui_password:
        result["hints"].append("Не заданы X3UI_USERNAME/X3UI_PASSWORD — часть сборок требует авторизации.")

    return result
