import logging
from pathlib import Path

from app.messaging.provider import MessengerProvider
from app.messaging.telegram.provider import TelegramProvider
from app.models import TgAccount

logger = logging.getLogger(__name__)


class SessionManager:
    """Пул Telethon-сессий — по одной на TG-аккаунт (менеджера).

    Связывает каждый аккаунт с ответственным менеджером. Ключ пула —
    ``account.id``; один аккаунт = одна сессия = один менеджер.
    """

    def __init__(self, api_id: int, api_hash: str, sessions_dir: str):
        self._api_id = api_id
        self._api_hash = api_hash
        self._sessions_dir = sessions_dir
        self._providers: dict[int, MessengerProvider] = {}

    def _build_provider(self, account: TgAccount) -> MessengerProvider:
        provider = TelegramProvider(self._api_id, self._api_hash, self._sessions_dir)
        # CRITICAL: per-account session subdirectory.
        # Иначе все провайдеры разделят один .session-файл
        # (<dir>/session) и менеджеры будут перезаписывать сессии друг друга.
        # session_file = _sessions_dir / "session", поэтому переопределяем
        # _sessions_dir на per-account подпапку ДО вызова connect().
        per_account_dir = Path(self._sessions_dir) / f"account_{account.id}"
        per_account_dir.mkdir(parents=True, exist_ok=True)
        provider._sessions_dir = per_account_dir  # type: ignore[attr-defined]
        return provider

    async def register(self, account: TgAccount) -> MessengerProvider:
        # Идемпотентно: если сессия уже зарегистрирована — возвращаем её.
        if account.id in self._providers:
            return self._providers[account.id]
        provider = self._build_provider(account)
        await provider.connect()
        self._providers[account.id] = provider
        logger.info(
            "Registered session for account_id=%s phone=%s",
            account.id,
            account.phone,
        )
        return provider

    def get(self, account_id: int) -> MessengerProvider | None:
        return self._providers.get(account_id)

    async def unregister(self, account_id: int) -> None:
        provider = self._providers.pop(account_id, None)
        if provider:
            await provider.disconnect()
            logger.info("Unregistered session for account_id=%s", account_id)

    async def close_all(self) -> None:
        for account_id in list(self._providers):
            await self.unregister(account_id)
