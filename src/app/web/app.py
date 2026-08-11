"""FastAPI application factory: маршруты + middleware + статика."""

import logging
from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import get_settings
from app.web.routes import dialogs, health, placement, templates, webhook
from app.web.session import create_session_cookie_params

logger = logging.getLogger(__name__)


def _parse_origins(raw: str) -> list[str]:
    """CORS_ORIGINS через запятую → список. '*' остаётся wildcard."""
    return [o.strip() for o in raw.split(",") if o.strip()]


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Bitrix-TG", version="0.1.0")

    # --- CORS ---
    origins = _parse_origins(settings.cors_origins)
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
    app.include_router(placement.router)
    app.include_router(dialogs.router)
    app.include_router(templates.router)

    # --- Dev-логин (только в dev-режиме) ---
    if settings.dev_mode:

        @app.get("/dev/login")
        async def dev_login(
            b24_user_id: int = Query(...),
            deal_id: int | None = Query(default=None),
        ):
            """Dev: выставить сессионную куку и открыть чат-страницу.

            В prod отключается (маршрут не регистрируется).
            """
            params = create_session_cookie_params(
                b24_user_id=b24_user_id, deal_id=deal_id,
                secret=settings.session_secret,
            )
            resp = RedirectResponse(url="/static/placement.html", status_code=302)
            resp.set_cookie(**params)
            return resp

    # --- Статика фронтенда ---
    static_path = Path(settings.static_dir)
    if static_path.is_dir():
        app.mount("/static", StaticFiles(directory=str(static_path)), name="static")
    else:
        logger.warning("Static dir not found: %s — /static не смонтирован", static_path)

    # --- Единый обработчик HTTPException → JSON ---
    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException):
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
            headers=getattr(exc, "headers", None),
        )

    return app
