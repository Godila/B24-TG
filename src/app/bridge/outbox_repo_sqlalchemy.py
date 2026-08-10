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

    async def fetch_due(self, limit: int = 50) -> list[OutboxItem]:
        now = datetime.now(UTC)
        stmt = (
            select(OutboxItem)
            .where(OutboxItem.status == OutboxStatus.queued)
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
