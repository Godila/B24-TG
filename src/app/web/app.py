"""FastAPI application factory: маршруты + middleware + статика."""

import logging
from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import get_settings
from app.web.routes import (
    admin,
    admin_api,
    bizproc,
    dialogs,
    health,
    inbox,
    openline,
    placement,
    public_media,
    templates,
    webhook,
)
from app.web.session import SESSION_COOKIE, create_session_cookie_params, verify_session

logger = logging.getLogger(__name__)


_UNREGISTERED_PAGE = """<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ЧатМост</title><link rel="icon" href="/static/brand/favicon.ico" sizes="48x48">
<style>
 body{{margin:0;font-family:-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
 background:#f2f4f8;color:#1c2733;display:flex;align-items:center;justify-content:center;
 height:100vh;font-size:14px}}
 .card{{background:#fff;border:1px solid #dfe3ea;border-radius:14px;max-width:440px;
 padding:24px;box-shadow:0 1px 3px rgba(20,30,50,.05)}}
 .mark{{height:44px;width:auto;flex:none;display:block;margin-bottom:14px}}
 h1{{font-size:17px;margin:0 0 8px}}
 p{{color:#66707d;line-height:1.5;margin:6px 0}}
 code{{background:#e8edf7;border-radius:6px;padding:2px 8px;color:#1f57c7;font-weight:600}}
</style></head><body><div class="card">
<img class="mark" src="/static/brand/logo-128x69.png" alt="" aria-hidden="true">
<h1>Вы не добавлены в ЧатМост</h1>
<p>Попросите администратора добавить вас: пункт «ЧатМост» в левом меню
Битрикс24 &rarr; вкладка «Панель» &rarr; раздел «Менеджеры».</p>
<p>Ваш ID пользователя Битрикс24: <code>#{user_id}</code> &mdash; сообщите его администратору.</p>
</div></body></html>"""

_SESSION_EXPIRED_PAGE = """<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ЧатМост</title><link rel="icon" href="/static/brand/favicon.ico" sizes="48x48">
<style>
 body{margin:0;font-family:-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
 background:#f2f4f8;color:#1c2733;display:flex;align-items:center;justify-content:center;
 height:100vh;font-size:14px}
 .card{background:#fff;border:1px solid #dfe3ea;border-radius:14px;max-width:440px;
 padding:24px;box-shadow:0 1px 3px rgba(20,30,50,.05)}
 .mark{height:44px;width:auto;flex:none;display:block;margin-bottom:14px}
 h1{font-size:17px;margin:0 0 8px}
 p{color:#66707d;line-height:1.5;margin:6px 0}
</style></head><body><div class="card">
<img class="mark" src="/static/brand/logo-128x69.png" alt="" aria-hidden="true">
<h1>Сессия истекла</h1>
<p>Закройте это окно и откройте «ЧатМост» заново из левого меню
Битрикс24 &mdash; сессия обновится автоматически.</p>
</div></body></html>"""


def _placement_unauthorized_page(request: Request) -> HTMLResponse:
    """Дружелюбная 401-страница для placement-iframe вместо сырого JSON.

    Placement-GET открывается внутри iframe (оболочка/прямые ссылки):
    невнесённый сотрудник видел бы голый JSON «Менеджер не найден».
    Различаем два случая по куке: подписана, но менеджера нет/неактивен
    → инструкция с его b24_user_id (сообщить админу); куки нет/протухла
    → «откройте заново из меню» (DESIGN.md: ошибка = направление).
    """
    token = request.cookies.get(SESSION_COOKIE)
    user_id = None
    if token:
        payload = verify_session(token, get_settings().session_secret)
        if payload and isinstance(payload.get("b24_user_id"), int):
            user_id = payload["b24_user_id"]
    if user_id is not None:
        return HTMLResponse(status_code=401, content=_UNREGISTERED_PAGE.format(user_id=user_id))
    return HTMLResponse(status_code=401, content=_SESSION_EXPIRED_PAGE)


def _parse_origins(raw: str) -> list[str]:
    """CORS_ORIGINS через запятую → список. '*' остаётся wildcard."""
    return [o.strip() for o in raw.split(",") if o.strip()]


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Bitrix-TG", version="0.1.0")

    # --- CORS ---
    # Fail-closed: без явно заданных CORS_ORIGINS middleware не подключается —
    # кросс-доменных запросов с credentials нет (только same-origin).
    origins = _parse_origins(settings.cors_origins)
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    # --- Маршруты ---
    app.include_router(health.router)
    app.include_router(webhook.router)
    # Активити бизнес-процессов (server-to-server, как webhook).
    app.include_router(bizproc.router)
    # События коннектора открытых линий (server-to-server, как webhook).
    app.include_router(openline.router)
    app.include_router(placement.router)
    app.include_router(dialogs.router)
    app.include_router(inbox.router)
    app.include_router(templates.router)
    # Админ-панель: страница + единый API (онбординг обоих каналов и
    # supervisor-панель). Каналы регистрируются сюда же.
    app.include_router(admin.router)
    app.include_router(admin_api.router)
    # Публичная share-страница подключения линии (без авторизации ЧатМост).
    from app.web.routes import connect

    app.include_router(connect.router)
    # Публичная раздача медиа по подписи (imconnector: B24 качает files[].url).
    app.include_router(public_media.router)
    from app.models import Messenger
    from app.onboarding.max_channel import MaxOnboardingChannel
    from app.onboarding.tg_channel import TgOnboardingChannel

    admin_api.register_channels(
        {
            Messenger.max: MaxOnboardingChannel(),
            Messenger.tg: TgOnboardingChannel(),
        }
    )

    # --- Dev-логин (только в dev-режиме) ---
    if settings.dev_mode:

        @app.get("/dev/login")
        async def dev_login(
            b24_user_id: int = Query(...),
            deal_id: int | None = Query(default=None),
            page: str = Query(default="chat"),
        ):
            """Dev: выставить сессионную куку и открыть страницу приложения.

            В prod отключается (маршрут не регистрируется).
            """
            target = {
                "chat": "/static/placement.html",
                "inbox": "/static/inbox.html",
            }.get(page, "/static/placement.html")
            params = create_session_cookie_params(
                b24_user_id=b24_user_id,
                deal_id=deal_id,
                secret=settings.session_secret,
                secure=not settings.dev_mode,
            )
            resp = RedirectResponse(url=target, status_code=302)
            resp.set_cookie(**params)
            return resp

    # --- Статика фронтенда ---
    static_path = Path(settings.static_dir)
    if static_path.is_dir():
        app.mount("/static", StaticFiles(directory=str(static_path)), name="static")
    else:
        logger.warning("Static dir not found: %s — /static не смонтирован", static_path)

    # --- Единый обработчик HTTPException → JSON ---
    # Исключение: 401 на placement-маршрутах — HTML-страница-инструкция
    # (iframe пользователя, а не API-клиент; см. _placement_unauthorized_page).
    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException):
        if exc.status_code == 401 and request.url.path.startswith("/placement"):
            return _placement_unauthorized_page(request)
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
            headers=getattr(exc, "headers", None),
        )

    return app
