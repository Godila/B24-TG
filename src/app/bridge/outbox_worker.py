"""OutboxWorker — воркер очереди исходящих сообщений.

Объединяет spec §6.1 слой 1 (throttle, анти-бан) и слой 2 (retry/backoff).
Воркер опрашивает очередь due-сообщений, для каждого элемента:
  1) находит провайдера для аккаунта (если нет — откладываем на 30с БЕЗ
     расхода попытки; дольше суток без провайдера — mark_failed);
  2) спрашивает throttler (если лимит исчерпан — короткий отклад на 10с,
     тоже без расхода попытки);
  3) отправляет через провайдера.

Дальнейшая судьба элемента зависит от SendResult:
  - success            -> mark_sent(external_message_id)
  - flood_wait_seconds -> reschedule(delay = flood_wait_seconds)
  - попытки >= max     -> mark_failed(error)
  - иначе              -> экспоненциальный backoff 30 * 2^attempts.

Попытки (attempts) расходуют только реальные (пусть и неудачные) попытки
отправки; безобидные отклонения (throttle/no_provider) идут с
``count_attempt=False``.

``OutboxRepository`` — абстракция доступа к данным; конкретная
SQLAlchemy-реализация появится в Фазе 2. Сейчас воркер тестируется через
mock'и репозитория/провайдера/throttler'а.
"""

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from app.models import OutboxItem

if TYPE_CHECKING:  # pragma: no cover - только для type-checker
    from app.bridge.throttler import Throttler
    from app.messaging.provider import MessengerProvider

logger = logging.getLogger(__name__)


class OutboxRepository:
    """Абстракция доступа к очереди outbox. Конкретная impl (SQLAlchemy) — в Фазе 2."""

    async def fetch_due(self, limit: int = 50) -> list[OutboxItem]:
        ...

    async def mark_sent(self, item: OutboxItem, external_message_id: int) -> None:
        ...

    async def mark_failed(self, item: OutboxItem, error: str) -> None:
        ...

    async def reschedule(
        self,
        item: OutboxItem,
        *,
        delay_seconds: int,
        error: str | None = None,
        count_attempt: bool = True,
    ) -> None:
        ...


def _no_provider_timed_out(created_at: datetime | None) -> bool:
    """Нет провайдера дольше суток — терминальная ошибка, а не вечный ретрай.

    ``created_at`` может быть naive (SQLite) или tz-aware (asyncpg);
    naive трактуем как UTC. None (не загружен/не установлен) — не таймаут.
    """
    if created_at is None:
        return False
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    return datetime.now(UTC) - created_at > timedelta(hours=24)


class OutboxWorker:
    """Воркер очереди outbox: throttle → send → retry/backoff.

    spec §6.1 слои 1+2.
    """

    def __init__(
        self,
        repo: OutboxRepository,
        get_provider: "Callable[[int], MessengerProvider | None]",
        throttler_factory: "Callable[[int], Throttler]",
        max_attempts: int = 5,
        poll_interval: int = 2,
        batch_size: int = 50,
    ):
        self._repo = repo
        self._get_provider = get_provider
        self._throttler_factory = throttler_factory
        self._max_attempts = max_attempts
        self._poll_interval = poll_interval
        self._batch_size = batch_size
        self._running = False
        self._throttlers: dict[int, Throttler] = {}

    # ------------------------------------------------------------------ #
    # Throttler pool (per-account)
    # ------------------------------------------------------------------ #
    def _throttler_for(self, account_id: int) -> "Throttler":
        """Один Throttler на аккаунт, лениво создаваемый и переиспользуемый."""
        if account_id not in self._throttlers:
            self._throttlers[account_id] = self._throttler_factory(account_id)
        return self._throttlers[account_id]

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def run(self) -> None:
        """Главный цикл: process → sleep → process → ... До ``stop()``."""
        self._running = True
        try:
            while self._running:
                try:
                    await self._process_once()
                except Exception:  # pragma: no cover - защитная сетка
                    logger.exception("OutboxWorker iteration failed; continuing")
                await asyncio.sleep(self._poll_interval)
        finally:
            self._running = False

    def stop(self) -> None:
        """Запросить остановку главного цикла (idempotent)."""
        self._running = False

    # ------------------------------------------------------------------ #
    # Single iteration
    # ------------------------------------------------------------------ #
    async def _process_once(self) -> None:
        """Достать due-элементы и обработать их по одному."""
        items = await self._repo.fetch_due(self._batch_size)
        for item in items:
            await self._handle(item)

    async def _handle(self, item: OutboxItem) -> None:
        """Полный pipeline решения для одного OutboxItem."""
        # 1. провайдер для аккаунта
        provider = self._get_provider(item.tg_account_id)
        if provider is None:
            # Отсутствие провайдера — не ошибка отправки: попытку не
            # расходуем. Но и бесконечно ретраить нельзя: если аккаунта
            # нет дольше суток — терминальный failed (no_provider_timeout).
            if _no_provider_timed_out(item.created_at):
                await self._repo.mark_failed(item, "no_provider_timeout")
                return
            await self._repo.reschedule(
                item, delay_seconds=30, error="no_provider", count_attempt=False
            )
            return

        # 2. throttle (анти-бан)
        throttler = self._throttler_for(item.tg_account_id)
        allowed = await throttler.acquire(is_initiation=bool(item.is_initiation))
        if not allowed:
            # лимит исчерпан — короткая пауза, попробуем скоро снова;
            # отправки не было, попытку не расходуем.
            await self._repo.reschedule(
                item, delay_seconds=10, error="throttled", count_attempt=False
            )
            return

        # 3. отправка
        result = await provider.send_message(
            account_id=item.tg_account_id,
            external_chat_id=item.external_chat_id,
            text=item.text or "",
            is_initiation=bool(item.is_initiation),
        )

        # 4. судьба по результату
        if result.success:
            await self._repo.mark_sent(item, result.external_message_id)
            return

        if result.flood_wait_seconds:
            # TG сам сказал, сколько подождать — уважаем.
            await self._repo.reschedule(
                item, delay_seconds=result.flood_wait_seconds, error="flood_wait"
            )
            return

        # Учитываем текущую попытку (после инкремента в БД это будет attempts+1).
        if item.attempts + 1 >= self._max_attempts:
            await self._repo.mark_failed(item, result.error or "unknown")
            return

        # экспоненциальный backoff: при attempts=0 -> 30, 1 -> 60, 2 -> 120, 3 -> 240, ...
        delay = 30 * (2 ** item.attempts)
        await self._repo.reschedule(item, delay_seconds=delay, error=result.error)
