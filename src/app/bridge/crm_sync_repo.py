"""SQLAlchemy-реализация CrmSyncRepository (по образцу outbox-repo).

``SqlAlchemyCrmSyncRepository`` работает в переданной сессии (мутаторы
коммитят сами; enqueue — нет, чтобы вызывающий мог сделать атомарную
транзакцию с сообщением). ``WorkerCrmSyncRepository`` — адаптер для
долгоживущего воркера: свежая сессия на каждый вызов.
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bridge.crm_sync_worker import CrmSyncData, CrmSyncRepository
from app.models import Contact, CrmSyncItem, CrmSyncStatus, Dialog, Manager, Message


class SqlAlchemyCrmSyncRepository(CrmSyncRepository):
    """CrmSyncRepository поверх одной AsyncSession."""

    def __init__(self, session: AsyncSession):
        self._session = session

    async def enqueue(self, *, kind: str, message_id: int) -> CrmSyncItem:
        """Поставить задачу CRM-синхронизации сообщения (status=queued).

        НЕ коммитит сам — вызывающий (handler/hook) управляет транзакцией;
        WorkerCrmSyncRepository ниже коммитит явно.
        """
        item = CrmSyncItem(
            kind=kind,
            message_id=message_id,
            status=CrmSyncStatus.queued,
            attempts=0,
            next_attempt_at=datetime.now(UTC),
        )
        self._session.add(item)
        await self._session.flush()
        return item

    async def fetch_due(self, limit: int = 20) -> list[CrmSyncItem]:
        # queued И retrying: reschedule() переводит в retrying, фильтр
        # только по queued навсегда подвесит отложенные задачи (урок outbox).
        now = datetime.now(UTC)
        stmt = (
            select(CrmSyncItem)
            .where(
                CrmSyncItem.status.in_(
                    [CrmSyncStatus.queued, CrmSyncStatus.retrying]
                )
            )
            .where(CrmSyncItem.next_attempt_at <= now)
            .order_by(CrmSyncItem.next_attempt_at)
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def mark_done(self, item: CrmSyncItem) -> None:
        await self._session.execute(
            update(CrmSyncItem)
            .where(CrmSyncItem.id == item.id)
            .values(status=CrmSyncStatus.done, last_error=None)
        )
        await self._session.commit()

    async def mark_failed(self, item: CrmSyncItem, error: str) -> None:
        await self._session.execute(
            update(CrmSyncItem)
            .where(CrmSyncItem.id == item.id)
            .values(
                status=CrmSyncStatus.failed,
                attempts=item.attempts + 1,
                last_error=error[:512],
            )
        )
        await self._session.commit()

    async def reschedule(
        self, item: CrmSyncItem, *, delay_seconds: int, error: str | None = None
    ) -> None:
        next_at = datetime.now(UTC) + timedelta(seconds=delay_seconds)
        await self._session.execute(
            update(CrmSyncItem)
            .where(CrmSyncItem.id == item.id)
            .values(
                status=CrmSyncStatus.retrying,
                attempts=item.attempts + 1,
                next_attempt_at=next_at,
                last_error=error,
            )
        )
        await self._session.commit()

    async def collect(self, message_id: int) -> CrmSyncData | None:
        """Message + Dialog + Contact + Manager одним запросом.

        None — сообщение (или его диалог/контакт) не найдено.
        """
        stmt = (
            select(
                Message.text,
                Contact.name,
                Contact.phone,
                Contact.crm_contact_id,
                Dialog.crm_deal_id,
                Dialog.crm_entity_type,
                Manager.b24_user_id,
            )
            .join(Dialog, Message.dialog_id == Dialog.id)
            .join(Contact, Dialog.contact_id == Contact.id)
            .outerjoin(Manager, Dialog.assigned_user_id == Manager.id)
            .where(Message.id == message_id)
        )
        row = (await self._session.execute(stmt)).one_or_none()
        if row is None:
            return None
        return CrmSyncData(
            message_text=row.text,
            sender_name=row.name,
            sender_phone=row.phone,
            crm_contact_id=row.crm_contact_id,
            crm_deal_id=row.crm_deal_id,
            crm_entity_type=row.crm_entity_type,
            assigned_b24_user_id=row.b24_user_id,
        )

    async def apply_inbound_result(
        self,
        message_id: int,
        *,
        contact_id: int | None,
        deal_id: int | None,
        timeline_comment_id: int | None,
    ) -> None:
        """Применить SyncResult к нашей БД.

        Обновляем только переданные поля (None — не трогаем), по цепочке
        Message -> Dialog -> Contact. crm_entity_type='deal' — так же, как
        это делал inbound-persist до выноса CRM из пути сообщения.
        """
        dialog_id = await self._session.scalar(
            select(Message.dialog_id).where(Message.id == message_id)
        )
        contact_pk = await self._session.scalar(
            select(Dialog.contact_id).where(Dialog.id == dialog_id)
        ) if dialog_id is not None else None

        if timeline_comment_id is not None:
            await self._session.execute(
                update(Message)
                .where(Message.id == message_id)
                .values(timeline_comment_id=timeline_comment_id)
            )
        if dialog_id is not None and deal_id is not None:
            await self._session.execute(
                update(Dialog)
                .where(Dialog.id == dialog_id)
                .values(crm_deal_id=deal_id, crm_entity_type="deal")
            )
        if contact_pk is not None and contact_id is not None:
            await self._session.execute(
                update(Contact)
                .where(Contact.id == contact_pk)
                .values(crm_contact_id=contact_id)
            )
        await self._session.commit()

    async def set_timeline_comment(self, message_id: int, comment_id: int) -> None:
        await self._session.execute(
            update(Message)
            .where(Message.id == message_id)
            .values(timeline_comment_id=comment_id)
        )
        await self._session.commit()


class WorkerCrmSyncRepository(CrmSyncRepository):
    """CrmSyncRepository, открывающий новую сессию на каждый вызов.

    См. WorkerOutboxRepository: воркер долгоживущий, делить сессию между
    итерациями небезопасно (detached-объекты, протухание транзакций).
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] | Callable[[], AsyncSession],
    ):
        self._session_factory = session_factory

    async def fetch_due(self, limit: int = 20) -> list[CrmSyncItem]:
        async with self._session_factory() as s:
            return await SqlAlchemyCrmSyncRepository(s).fetch_due(limit)

    async def mark_done(self, item: CrmSyncItem) -> None:
        async with self._session_factory() as s:
            await SqlAlchemyCrmSyncRepository(s).mark_done(item)

    async def mark_failed(self, item: CrmSyncItem, error: str) -> None:
        async with self._session_factory() as s:
            await SqlAlchemyCrmSyncRepository(s).mark_failed(item, error)

    async def reschedule(
        self, item: CrmSyncItem, *, delay_seconds: int, error: str | None = None
    ) -> None:
        async with self._session_factory() as s:
            await SqlAlchemyCrmSyncRepository(s).reschedule(
                item, delay_seconds=delay_seconds, error=error
            )

    async def enqueue(self, *, kind: str, message_id: int) -> CrmSyncItem:
        # В отличие от SqlAlchemyCrmSyncRepository.enqueue, здесь коммитим
        # сами: вызывающий (handler/hook) не управляет сессией адаптера.
        async with self._session_factory() as s:
            inner = SqlAlchemyCrmSyncRepository(s)
            item = await inner.enqueue(kind=kind, message_id=message_id)
            await s.commit()
            return item

    async def collect(self, message_id: int) -> CrmSyncData | None:
        async with self._session_factory() as s:
            return await SqlAlchemyCrmSyncRepository(s).collect(message_id)

    async def apply_inbound_result(
        self,
        message_id: int,
        *,
        contact_id: int | None,
        deal_id: int | None,
        timeline_comment_id: int | None,
    ) -> None:
        async with self._session_factory() as s:
            await SqlAlchemyCrmSyncRepository(s).apply_inbound_result(
                message_id,
                contact_id=contact_id,
                deal_id=deal_id,
                timeline_comment_id=timeline_comment_id,
            )

    async def set_timeline_comment(self, message_id: int, comment_id: int) -> None:
        async with self._session_factory() as s:
            await SqlAlchemyCrmSyncRepository(s).set_timeline_comment(
                message_id, comment_id
            )
