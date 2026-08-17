"""Concrete OutboxRepository на SQLAlchemy 2.0 async."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.bridge.outbox_worker import OutboxRepository
from app.models import Message, MessageStatus, OutboxItem, OutboxStatus


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
        message_id: int | None = None,
        attachment_id: int | None = None,
    ) -> OutboxItem:
        """Поставить новое сообщение в очередь отправки (status=queued).

        ``message_id`` связывает элемент очереди с Message(direction=outbound):
        по нему mark_sent/mark_failed закрывают статус исходящего сообщения.
        ``attachment_id`` — медиа-вложение элемента (отправка файлом).

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
            message_id=message_id,
            attachment_id=attachment_id,
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
            # Вложение элемента: воркер читает его метаданные сразу,
            # lazy-доступ в async-контексте (после закрытия сессии
            # адаптера) роняет MissingGreenlet.
            .options(selectinload(OutboxItem.attachment))
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

    async def mark_sent(self, item: OutboxItem, external_message_id: str) -> None:
        # Одна транзакция: outbox -> sent и связанный Message -> sent
        # (+ external_message_id для идемпотентности и sent_at для UI).
        await self._session.execute(
            update(OutboxItem)
            .where(OutboxItem.id == item.id)
            .values(status=OutboxStatus.sent)
        )
        if item.message_id:
            await self._session.execute(
                update(Message)
                .where(Message.id == item.message_id)
                .values(
                    status=MessageStatus.sent,
                    external_message_id=external_message_id or None,
                    sent_at=func.now(),
                )
            )
        await self._session.commit()

    async def mark_failed(self, item: OutboxItem, error: str) -> None:
        # Одна транзакция: outbox -> failed и Message -> error,
        # иначе исходящее навсегда зависнет в pending (⏳ в UI).
        await self._session.execute(
            update(OutboxItem)
            .where(OutboxItem.id == item.id)
            .values(status=OutboxStatus.failed, last_error=error)
        )
        if item.message_id:
            await self._session.execute(
                update(Message)
                .where(Message.id == item.message_id)
                .values(status=MessageStatus.error)
            )
        await self._session.commit()

    async def reschedule(
        self,
        item: OutboxItem,
        *,
        delay_seconds: int,
        error: str | None = None,
        count_attempt: bool = True,
    ) -> None:
        # count_attempt=False — безобидные отклонения (throttle/no_provider):
        # попытка не расходуется, иначе 4 отклонения + первая реальная ошибка
        # исчерпают лимит и сообщение упадёт в failed без единой отправки.
        next_at = datetime.now(UTC) + timedelta(seconds=delay_seconds)
        values: dict = {
            "status": OutboxStatus.retrying,
            "next_attempt_at": next_at,
            "last_error": error,
        }
        if count_attempt:
            values["attempts"] = OutboxItem.attempts + 1
        await self._session.execute(
            update(OutboxItem).where(OutboxItem.id == item.id).values(**values)
        )
        await self._session.commit()
