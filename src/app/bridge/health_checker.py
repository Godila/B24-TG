"""Периодическая проверка сессий: логи + алерты админу, БД не трогает.

План 009 в исходном виде персистил ``tg_accounts.status`` по факту
подключения — инцидент 08-18 показал дыру: транзиентный обрыв → чекер
писал offline → аккаунт с ЖИВЫМ токеном выпадал из active-сета
AccountSyncWorker'а навсегда (offline никто не пробует регистрировать).
Статус — зона терминальных auth-отказов (failure-hook) и QR-флоу; чекер —
наблюдатель: алерт на переходе в дисконнект (память процесса,
transition-only) и лог восстановления. ``session_factory`` остался только
для read-подбора канала/телефона в тексте алерта.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bridge.session_manager import SessionManager
from app.models import TgAccount

logger = logging.getLogger(__name__)

# Колбэк алерта: async def admin_alert(user_id: int, text: str) -> None.
# Реализуется в main.py (TokenManager → ImService.send_notification).
AlertNotifier = Callable[[int, str], Awaitable[None]]


class HealthChecker:
    """Периодическая проверка сессий.

    Без ``notifier`` — только логирование. С ним — алерт админу на
    переходе подключён→отключён (повторные проверки дисконнекта не
    спамят; после реконнекта молча сбрасываются).
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
        #: Последнее наблюдение (отсутствие строки = «был подключён»:
        #: мёртвая со старта сессия алертнёт на первом же тике).
        self._was_connected: dict[int, bool] = {}

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
                was = self._was_connected.get(account_id, True)
                self._was_connected[account_id] = connected
                if connected:
                    if not was:
                        logger.info("Account %s: connection restored", account_id)
                    continue
                logger.warning("Account %s: session disconnected", account_id)
                if was:
                    await self._alert(account_id)
            except Exception:
                logger.exception("Health check failed for account %s", account_id)

    async def _alert(self, account_id: int) -> None:
        """Алерт админу; канал/телефон — read-only из БД (метаданные)."""
        if self._notifier is None:
            return
        channel = phone = "?"
        if self._session_factory is not None:
            async with self._session_factory() as session:
                account = (
                    await session.execute(
                        select(TgAccount).where(TgAccount.id == account_id)
                    )
                ).scalar_one_or_none()
                if account is not None:
                    channel = account.messenger.value
                    phone = account.phone
        try:
            await self._notifier(
                self._admin_user_id,
                f"⚠️ ЧатМост: {channel.upper()}-аккаунт id={account_id} ({phone}) "
                "отключён — проверьте сессию",
            )
        except Exception:
            # Сбой доставки алерта не должен ронять чекер.
            logger.exception("Admin alert failed for account %s", account_id)
