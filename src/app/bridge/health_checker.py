import asyncio
import logging

from app.bridge.session_manager import SessionManager

logger = logging.getLogger(__name__)


class HealthChecker:
    """Периодическая проверка сессий (spec §6.1 слой 3).
    В Фазе 1 — только логирование; алерты админу в Фазе 4."""

    def __init__(self, session_manager: SessionManager, interval_sec: int = 300):
        self._sm = session_manager
        self._interval = interval_sec
        self._running = False

    async def run(self) -> None:
        self._running = True
        while self._running:
            await asyncio.sleep(self._interval)
            await self._check_once()

    def stop(self) -> None:
        self._running = False

    async def _check_once(self) -> None:
        for account_id, provider in list(self._sm._providers.items()):  # type: ignore[attr-defined]
            try:
                # Унифицированный интерфейс: проверяем наличие подключения
                client = getattr(provider, "_client", None)
                connected = bool(client and getattr(client, "is_connected", lambda: False)())
                if not connected:
                    logger.warning("Account %s: session disconnected", account_id)
            except Exception:
                logger.exception("Health check failed for account %s", account_id)
