"""Concrete OutboxRepository на SQLAlchemy 2.0 async."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.bridge.outbox_worker import OutboxRepository
from app.models import OutboxItem, OutboxStatus


class SqlAlchemyOutboxRepository(OutboxRepository):
    """SQLAlchemy-реализация очереди outbox."""

    def __init__(self, session: AsyncSession):
        self._session = session

    async def enqueue(
        self,
        *,
        dialog_id: int,
        tg_account_id: int,
        external_chat_id: str,
        text: str,
        is_initiation: bool = False,
    ) -> OutboxItem:
        """Поставить новое сообщение в очередь отправки (status=queued).

        Внимание: метод НЕ коммитит сам — это делает вызывающий (route),
        чтобы вставка Message и OutboxItem прошла в одной транзакции
        (атомарность: либо обе записи, либо ни одной).
        """
        item = OutboxItem(
            dialog_id=dialog_id,
            tg_account_id=tg_account_id,
            external_chat_id=external_chat_id,
            text=text,
            is_initiation=is_initiation,
            status=OutboxStatus.queued,
            attempts=0,
            next_attempt_at=datetime.now(UTC),
        )
        self._session.add(item)
        await self._session.flush()
        return item

    async def fetch_due(self, limit: int = 50) -> list[OutboxItem]:
        # ВАЖНО: выбираем и queued, и retrying. reschedule() ставит статус
        # retrying; если фильтровать только по queued, отложенные сообщения
        # навсегда зависнут в очереди (OutboxWorker вызывает reschedule на
        # каждом нетерминальном исходе: throttle/flood_wait/backoff/no_provider).
        now = datetime.now(UTC)
        stmt = (
            select(OutboxItem)
            .where(
                OutboxItem.status.in_(
                    [OutboxStatus.queued, OutboxStatus.retrying]
                )
            )
            .where(OutboxItem.next_attempt_at <= now)
            .order_by(OutboxItem.next_attempt_at)
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def mark_sent(self, item: OutboxItem, external_message_id: int) -> None:
        # В таблице outbox нет столбца под external_message_id — он хранится
        # в записи Message. Здесь лишь фиксируем успешную доставку.
        await self._session.execute(
            update(OutboxItem)
            .where(OutboxItem.id == item.id)
            .values(status=OutboxStatus.sent)
        )
        await self._session.commit()

    async def mark_failed(self, item: OutboxItem, error: str) -> None:
        await self._session.execute(
            update(OutboxItem)
            .where(OutboxItem.id == item.id)
            .values(status=OutboxStatus.failed, last_error=error)
        )
        await self._session.commit()

    async def reschedule(
        self, item: OutboxItem, *, delay_seconds: int, error: str | None = None
    ) -> None:
        next_at = datetime.now(UTC) + timedelta(seconds=delay_seconds)
        await self._session.execute(
            update(OutboxItem)
            .where(OutboxItem.id == item.id)
            .values(
                status=OutboxStatus.retrying,
                attempts=OutboxItem.attempts + 1,
                next_attempt_at=next_at,
                last_error=error,
            )
        )
        await self._session.commit()
