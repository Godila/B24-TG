"""Webhook-обработчики Bitrix24: ONAPPINSTALL и события."""

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.b24.token_manager import TokenManager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhook/b24", tags=["bitrix24"])


def get_token_manager() -> TokenManager:
    """Factory для DI; в production поднимается с config. См. Task 10 wiring."""
    from app.config import get_settings

    s = get_settings()
    return TokenManager(client_id=s.b24_client_id, client_secret=s.b24_client_secret)


@router.post("/onappinstall")
async def on_app_install(request: Request) -> JSONResponse:
    """Обработчик события установки приложения.

    Bitrix24 присылает OAuth-токены в POST body (поле auth).
    """
    payload = await request.json()
    auth_data = payload.get("auth", {})
    logger.info(
        "ONAPPINSTALL received: member_id=%s", auth_data.get("member_id"),
    )

    tm = get_token_manager()
    await tm.save_install_data(auth_data)

    return JSONResponse({"status": "ok"})
