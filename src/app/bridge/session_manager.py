"""Пул провайдеров канальных аккаунтов — по одному на аккаунт (менеджера).

Фабрики поставляются словарём ``builders``: ``{Messenger: factory(account) ->
MessengerProvider}``. Это точка расширения для новых каналов: TelegramProvider
строится дефолтной фабрикой из TG-настроек, MaxUserProvider — фабрикой из
``app.messaging.max.factory`` (wiring в ``main.py``).

Ключ пула — ``account.id`` (уникален глобально в tg_accounts, включая
MAX-строки), поэтому outbox-маршрутизация и throttler-pool не различают каналы.
"""

import asyncio
import logging
from collections.abc import Callable, Iterable
from pathlib import Path

from app.messaging.provider import MessengerProvider
from app.messaging.telegram.provider import TelegramProvider
from app.models import Messenger, TgAccount

logger = logging.getLogger(__name__)

ProviderBuilder = Callable[[TgAccount], MessengerProvider]


class SessionManager:
    def __init__(self, api_id: int, api_hash: str, sessions_dir: str,
                 proxy: tuple | None = None, *,
                 builders: dict[Messenger, ProviderBuilder] | None = None,
                 register_timeout_sec: float = 60.0):
        self._api_id = api_id
        self._api_hash = api_hash
        self._sessions_dir = sessions_dir
        self._proxy = proxy
        self._providers: dict[int, MessengerProvider] = {}
        self._builders: dict[Messenger, ProviderBuilder] = builders or {}
        self._register_timeout_sec = register_timeout_sec
        # TG остаётся дефолтным каналом (обратная совместимость с тестами,
        # которые не передают builders).
        self._builders.setdefault(Messenger.tg, self._default_tg_builder)

    def _default_tg_builder(self, account: TgAccount) -> MessengerProvider:
        # CRITICAL: per-account session subdirectory.
        # Иначе все провайдеры разделят один .session-файл
        # (<dir>/session) и менеджеры будут перезаписывать сессии друг друга.
        # session_file = <dir>/session, поэтому dir = per-account подпапка.
        per_account_dir = Path(self._sessions_dir) / f"account_{account.id}"
        return TelegramProvider(
            self._api_id, self._api_hash, per_account_dir, proxy=self._proxy
        )

    def _build_provider(self, account: TgAccount) -> MessengerProvider:
        try:
            builder = self._builders[account.messenger]
        except KeyError:
            raise ValueError(
                f"no provider builder for messenger={account.messenger}"
            ) from None
        return builder(account)

    async def register(self, account: TgAccount) -> MessengerProvider:
        # Идемпотентно: если сессия уже зарегистрирована — возвращаем её.
        if account.id in self._providers:
            return self._providers[account.id]
        provider = self._build_provider(account)
        # Подключение с таймаутом: Telethon не имеет своего RPC-таймаута,
        # а старт bridge ждёт регистрации — без лимита мёртвый MTProto-
        # прокси подвешивает весь процесс (воркеры не поднимаются).
        try:
            await asyncio.wait_for(
                provider.connect(), timeout=self._register_timeout_sec
            )
        except TimeoutError:
            await provider.disconnect()
            logger.error(
                "Register timeout (%ss) account_id=%s messenger=%s — "
                "провайдер не подключился, продолжаем без него",
                self._register_timeout_sec, account.id, account.messenger.value,
            )
            raise
        self._providers[account.id] = provider
        logger.info(
            "Registered session for account_id=%s messenger=%s phone=%s",
            account.id,
            account.messenger.value,
            account.phone,
        )
        return provider

    def get(self, account_id: int) -> MessengerProvider | None:
        return self._providers.get(account_id)

    def registered_ids(self) -> set[int]:
        """ID аккаунтов с живыми провайдерами (для AccountSyncWorker)."""
        return set(self._providers)

    def iter_providers(self) -> Iterable[tuple[int, MessengerProvider]]:
        """Снимок пула (для HealthChecker — без залезания в приватные поля)."""
        return list(self._providers.items())

    async def unregister(self, account_id: int) -> None:
        provider = self._providers.pop(account_id, None)
        if provider:
            await provider.disconnect()
            logger.info("Unregistered session for account_id=%s", account_id)

    async def close_all(self) -> None:
        for account_id in list(self._providers):
            await self.unregister(account_id)
