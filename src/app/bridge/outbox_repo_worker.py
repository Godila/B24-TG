"""WorkerOutboxRepository — адаптер OutboxRepository для OutboxWorker.

``SqlAlchemyOutboxRepository`` рассчитан на session-per-request: каждая
мутация коммитит и закрывает свою сессию. ``OutboxWorker`` же долгоживущий и
опрашивает очередь циклически — делить одну сессию между итерациями
небезопасно (detached-объекты, протухание транзакций).

Здесь оборачиваем фабрику сессий: каждый метод открывает *свежую* сессию,
делегирует работу ``SqlAlchemyOutboxRepository`` и возвращает результат.
Подкласс ``OutboxRepository`` — drop-in для воркера.
"""

from collections.abc import Callable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bridge.outbox_repo_sqlalchemy import SqlAlchemyOutboxRepository
from app.bridge.outbox_worker import OutboxRepository
from app.models import OutboxItem


class WorkerOutboxRepository(OutboxRepository):
    """OutboxRepository, открывающий новую сессию на каждый вызов."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession] | Callable[[], AsyncSession]):
        self._session_factory = session_factory

    async def fetch_due(self, limit: int = 50) -> list[OutboxItem]:
        async with self._session_factory() as s:
            return await SqlAlchemyOutboxRepository(s).fetch_due(limit)

    async def mark_sent(self, item: OutboxItem, external_message_id: str) -> None:
        async with self._session_factory() as s:
            await SqlAlchemyOutboxRepository(s).mark_sent(item, external_message_id)

    async def mark_failed(self, item: OutboxItem, error: str) -> None:
        async with self._session_factory() as s:
            await SqlAlchemyOutboxRepository(s).mark_failed(item, error)

    async def reschedule(
        self,
        item: OutboxItem,
        *,
        delay_seconds: int,
        error: str | None = None,
        count_attempt: bool = True,
    ) -> None:
        async with self._session_factory() as s:
            await SqlAlchemyOutboxRepository(s).reschedule(
                item,
                delay_seconds=delay_seconds,
                error=error,
                count_attempt=count_attempt,
            )

    async def enqueue(
        self,
        *,
        dialog_id: int,
        tg_account_id: int,
        external_chat_id: str,
        text: str,
        is_initiation: bool = False,
        message_id: int | None = None,
    ) -> OutboxItem:
        # В отличие от SqlAlchemyOutboxRepository.enqueue, здесь коммитим сами:
        # вызывающий (воркер/роут) не управляет сессией адаптера.
        async with self._session_factory() as s:
            inner = SqlAlchemyOutboxRepository(s)
            item = await inner.enqueue(
                dialog_id=dialog_id,
                tg_account_id=tg_account_id,
                external_chat_id=external_chat_id,
                text=text,
                is_initiation=is_initiation,
                message_id=message_id,
            )
            await s.commit()
            return item
