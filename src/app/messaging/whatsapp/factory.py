"""Фабрика WhatsAppProvider: собирает провайдер из полей аккаунта + Settings."""

import logging

from app.config import get_settings
from app.media.storage import MediaStorage
from app.messaging.whatsapp.api import OpenWaClient, wa_logout_and_delete
from app.messaging.whatsapp.media import WaMedia
from app.messaging.whatsapp.provider import WhatsAppProvider
from app.models import TgAccount

logger = logging.getLogger(__name__)


def build_wa_provider(
    account: TgAccount, *, media_storage: MediaStorage | None = None
) -> WhatsAppProvider:
    """Builder для SessionManager: (messenger=wa) аккаунт → провайдер.

    Креды — из строки аккаунта (wa_session_id с QR-онбординга), транспорт —
    из настроек (base_url/api_key сайдкара). ``media_storage`` замыкается
    через partial в main.py. None = медиа выключено (тесты).
    """
    settings = get_settings()
    if not account.wa_session_id:
        raise ValueError(
            f"WA-аккаунт id={account.id} без wa_session_id — нужен QR-онбординг"
        )
    if not settings.wa_api_key:
        raise ValueError("wa_api_key не задан — канал WhatsApp не сконфигурирован")
    api = OpenWaClient(
        base_url=settings.wa_base_url,
        api_key=settings.wa_api_key,
        timeout=settings.wa_request_timeout_sec,
    )
    media = WaMedia(api=api, storage=media_storage) if media_storage else None
    return WhatsAppProvider(
        session_id=account.wa_session_id,
        api=api,
        media=media,
        base_url=settings.wa_base_url,
        api_key=settings.wa_api_key,
    )


async def cleanup_wa_session(session_id: str | None) -> None:
    """Logout+delete сессии в OpenWA (unlink/удаление линии): best-effort,
    None-safe (линия без сессии — тихий no-op)."""
    if not session_id:
        return
    settings = get_settings()
    if not settings.wa_api_key:
        return
    await wa_logout_and_delete(
        OpenWaClient(
            base_url=settings.wa_base_url,
            api_key=settings.wa_api_key,
            timeout=settings.wa_request_timeout_sec,
        ),
        session_id,
    )
