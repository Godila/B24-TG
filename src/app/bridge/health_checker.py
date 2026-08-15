"""Периодическая проверка TG-сессий (spec §6.1 слой 3).

План 009: чекер не только логирует, но и персистит реальный статус в
``tg_accounts.status`` (его читает web-процесс в ``/health``), а на переходе
active→offline шлёт алерт админу в B24-чат (ImService.notify_manager).
Алерты transition-only: повторные проверки уже офлайн-аккаунта не спамят.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bridge.session_manager import SessionManager
from app.models import TgAccount, TgAccountStatus

logger = logging.getLogger(__name__)

# Колбэк алерта: async def admin_alert(user_id: int, text: str) -> None.
# Реализуется в main.py (TokenManager → ImService.notify_manager).
AlertNotifier = Callable[[int, str], Awaitable[None]]


class HealthChecker:
    """Периодическая проверка сессий.

    Без ``session_factory``/``notifier`` работает как в Фазе 1 — только
    логирование (обратная совместимость). С ними — персистит статусы в БД
    и алертит админа при отключении аккаунта.
    """

    def __init__(
        self,
        session_manager: SessionManager,
        interval_sec: int = 300,
        *,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        notifier: AlertNotifier | None = None,
        admin_user_id: int | None = None,
    ):
        self._sm = session_manager
        self._interval = interval_sec
        self._session_factory = session_factory
        self._notifier = notifier
        self._admin_user_id = admin_user_id
        self._running = False

    async def run(self) -> None:
        self._running = True
        while self._running:
            await asyncio.sleep(self._interval)
            await self._check_once()

    def stop(self) -> None:
        self._running = False

    async def _check_once(self) -> None:
        for account_id, provider in self._sm.iter_providers():
            try:
                connected = provider.is_connected()
                if not connected:
                    logger.warning("Account %s: session disconnected", account_id)
                if self._session_factory is not None:
                    await self._persist_status(account_id, connected)
            except Exception:
                logger.exception("Health check failed for account %s", account_id)

    async def _persist_status(self, account_id: int, connected: bool) -> None:
        """Обновляет ``tg_accounts.status`` по факту подключения.

        Алерт — только на переходе active→offline (transition-only):
        пока БД говорит active, отключение — событие; повторные проверки
        уже офлайн-аккаунта молчат. Обратный переход offline→active —
        просто UPDATE без алерта.
        """
        assert self._session_factory is not None  # narrows for type-checkers
        new_status = TgAccountStatus.active if connected else TgAccountStatus.offline
        async with self._session_factory() as session:
            account = (
                await session.execute(select(TgAccount).where(TgAccount.id == account_id))
            ).scalar_one_or_none()
            if account is None:
                logger.warning("Health check: account %s not found in DB", account_id)
                return
            old_status = account.status
            phone = account.phone
            channel = account.messenger.value
            if old_status == new_status:
                return  # статус не изменился — не трогаем БД и не алертим
            account.status = new_status
            await session.commit()

        if new_status is TgAccountStatus.offline:
            logger.error("Account %s went offline", account_id)
            if old_status == TgAccountStatus.active:
                await self._alert(account_id, channel, phone)
        else:
            logger.info("Account %s is back online", account_id)

    async def _alert(self, account_id: int, channel: str, phone: str) -> None:
        if self._notifier is None:
            return
        try:
            await self._notifier(
                self._admin_user_id,
                f"⚠️ Bitrix-TG: {channel.upper()}-аккаунт id={account_id} ({phone}) "
                "отключён — проверьте сессию",
            )
        except Exception:
            # Сбой доставки алерта не должен ронять чекер.
            logger.exception("Admin alert failed for account %s", account_id)
