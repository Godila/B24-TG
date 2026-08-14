"""Webhook-обработчики Bitrix24: ONAPPINSTALL и события."""

import hmac
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.b24.token_manager import TokenManager
from app.config import get_settings
from app.web.schemas import OnAppInstallAuth

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhook/b24", tags=["bitrix24"])


def get_token_manager() -> TokenManager:
    """Factory для DI; в production поднимается с config. См. Task 10 wiring."""
    s = get_settings()
    return TokenManager(client_id=s.b24_client_id, client_secret=s.b24_client_secret)


@router.post("/onappinstall")
async def on_app_install(request: Request) -> JSONResponse:
    """Обработчик события установки приложения.

    Bitrix24 присылает OAuth-токены в POST body (поле auth).

    Безопасность: без секретного заголовка запрос отвергается. Bitrix24
    НЕ умеет подписывать ONAPPINSTALL и не передаёт наш заголовок — реальные
    install-вызовы с портала получат 401. Это осознанный trade-off: endpoint
    используется вручную (при переустановке приложения) — передай заголовок
    ``X-Webhook-Secret`` со значением ``B24_WEBHOOK_SECRET`` из .env.
    """
    settings = get_settings()
    secret = request.headers.get("X-Webhook-Secret", "")
    if not settings.b24_webhook_secret or not hmac.compare_digest(
        secret, settings.b24_webhook_secret
    ):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    payload = await request.json()
    auth_raw = payload.get("auth", {}) if isinstance(payload, dict) else {}
    try:
        auth = OnAppInstallAuth.model_validate(auth_raw)
    except ValidationError:
        logger.warning("ONAPPINSTALL: invalid auth payload rejected")
        return JSONResponse({"error": "validation error"}, status_code=422)

    logger.info("ONAPPINSTALL received: member_id=%s", auth.member_id)

    tm = get_token_manager()
    await tm.save_install_data(auth.model_dump())

    return JSONResponse({"status": "ok"})
