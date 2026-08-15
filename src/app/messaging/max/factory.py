"""Фабрика MaxUserProvider: собирает провайдер из полей аккаунта + Settings."""

import logging

from app.config import get_settings
from app.messaging.max.protocol import (
    DEFAULT_APP_VERSION,
    DEFAULT_BROWSER_UA,
    DEFAULT_ORIGIN,
    DEFAULT_WS_URL,
    build_user_agent,
)
from app.messaging.max.provider import MaxUserProvider
from app.messaging.max.ws_client import MaxWsClient
from app.models import TgAccount

logger = logging.getLogger(__name__)


def build_max_provider(account: TgAccount) -> MaxUserProvider:
    """Builder для SessionManager: (messenger=max) аккаунт → провайдер.

    Креды — из строки аккаунта (token/device_id с QR-онбординга), транспорт —
    из настроек (ws_url/app_version/UA дрейфуют независимо от аккаунтов).
    Аккаунт без токена — ошибка конфигурации (не должен попадать в active).
    """
    settings = get_settings()
    if not account.token or not account.device_id:
        raise ValueError(
            f"MAX-аккаунт id={account.id} без token/device_id — нужен QR-онбординг"
        )
    return MaxUserProvider(
        token=account.token,
        device_id=account.device_id,
        own_user_id=account.max_user_id,
        ws_url=settings.max_ws_url or DEFAULT_WS_URL,
        headers=max_headers_for_onboarding(),
        user_agent=build_user_agent(
            settings.max_app_version or DEFAULT_APP_VERSION,
            settings.max_browser_ua or DEFAULT_BROWSER_UA,
        ),
        request_timeout=settings.max_request_timeout_sec,
        heartbeat_idle_sec=settings.max_heartbeat_idle_sec,
        heartbeat_tick_sec=settings.max_heartbeat_tick_sec,
        backoff_min_sec=settings.max_backoff_min_sec,
        backoff_max_sec=settings.max_backoff_max_sec,
    )


def max_headers_for_onboarding() -> dict[str, str]:
    """Заголовки WS-соединения QR-онбординга (тот же Origin/UA)."""
    settings = get_settings()
    return {
        "Origin": settings.max_origin or DEFAULT_ORIGIN,
        "User-Agent": settings.max_browser_ua or DEFAULT_BROWSER_UA,
    }


def make_onboarding_client() -> MaxWsClient:
    """Короткоживущий клиент для QR-флоу в web-процессе (/admin/max)."""
    settings = get_settings()
    return MaxWsClient(
        url=settings.max_ws_url or DEFAULT_WS_URL,
        headers=max_headers_for_onboarding(),
        request_timeout=settings.max_request_timeout_sec,
    )
