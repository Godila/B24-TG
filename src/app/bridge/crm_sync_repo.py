"""SQLAlchemy-реализация CrmSyncRepository (по образцу outbox-repo).

``SqlAlchemyCrmSyncRepository`` работает в переданной сессии (мутаторы
коммитят сами; enqueue — нет, чтобы вызывающий мог сделать атомарную
транзакцию с сообщением). ``WorkerCrmSyncRepository`` — адаптер для
долгоживущего воркера: свежая сессия на каждый вызов.
"""

import json
import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.b24.sources import SOURCE_ID_RE
from app.b24.sync import CRM_MODE_DEFAULT, CRM_MODES, TIMELINE_MODE_DEFAULT, TIMELINE_MODES
from app.bridge.crm_sync_worker import AttachmentMeta, CrmSyncData, CrmSyncRepository
from app.models import (
    AccountMember,
    AppSetting,
    Attachment,
    Contact,
    CrmSyncItem,
    CrmSyncStatus,
    Dialog,
    Manager,
    Message,
    Messenger,
)

logger = logging.getLogger(__name__)

TIMELINE_MODE_KEY = "timeline_mode"
#: Грузить ли файлы вложений в timeline-комментарии CRM (FILES у
#: crm.timeline.comment.add): "on" | "off". По умолчанию выключено —
#: диск/квота портала B24 дороже текст-метки «[фото]».
MEDIA_TO_TIMELINE_KEY = "media_to_timeline"
#: Какие CRM-карточки заводить новым клиентам: "deal" (контакт+сделка)
#: или "lead" (только лид). По умолчанию "deal" — прежнее поведение.
CRM_MODE_KEY = "crm_mode"
#: Маппинг канал→код записи справочника источников, JSON {"tg": "...", "max": ""}
#: (панель «Настройки»). Нет ключа/строки → дефолт канала; "" → не передавать.
SOURCE_MAP_KEY = "source_map"


async def _read_setting(session_factory, key: str) -> str | None:
    """Прочитать ключ app_settings (нет строки — None)."""
    async with session_factory() as s:
        row = (
            await s.execute(select(AppSetting).where(AppSetting.key == key))
        ).scalar_one_or_none()
    return row.value if row is not None else None


async def _upsert_setting(session_factory, key: str, value: str) -> None:
    """Upsert ключа app_settings."""
    async with session_factory() as s:
        row = (
            await s.execute(select(AppSetting).where(AppSetting.key == key))
        ).scalar_one_or_none()
        if row is None:
            s.add(AppSetting(key=key, value=value))
        else:
            row.value = value
        await s.commit()


async def get_timeline_mode(session_factory) -> str:
    """Прочитать app_settings.timeline_mode (нет строки/мусор — дефолт)."""
    value = await _read_setting(session_factory, TIMELINE_MODE_KEY)
    return value if value in TIMELINE_MODES else TIMELINE_MODE_DEFAULT


async def set_timeline_mode(session_factory, mode: str) -> None:
    """Upsert app_settings.timeline_mode (режим обязан быть из TIMELINE_MODES)."""
    if mode not in TIMELINE_MODES:
        raise ValueError(f"bad timeline_mode: {mode!r}")
    await _upsert_setting(session_factory, TIMELINE_MODE_KEY, mode)


async def get_media_to_timeline(session_factory) -> bool:
    """Прочитать app_settings.media_to_timeline (нет строки/мусор — выкл)."""
    return await _read_setting(session_factory, MEDIA_TO_TIMELINE_KEY) == "on"


async def set_media_to_timeline(session_factory, enabled: bool) -> None:
    """Upsert app_settings.media_to_timeline."""
    await _upsert_setting(session_factory, MEDIA_TO_TIMELINE_KEY, "on" if enabled else "off")


async def get_crm_mode(session_factory) -> str:
    """Прочитать app_settings.crm_mode (нет строки/мусор — дефолт 'deal')."""
    value = await _read_setting(session_factory, CRM_MODE_KEY)
    return value if value in CRM_MODES else CRM_MODE_DEFAULT


async def set_crm_mode(session_factory, mode: str) -> None:
    """Upsert app_settings.crm_mode (режим обязан быть из CRM_MODES)."""
    if mode not in CRM_MODES:
        raise ValueError(f"bad crm_mode: {mode!r}")
    await _upsert_setting(session_factory, CRM_MODE_KEY, mode)


_SOURCE_ID_RE = re.compile(SOURCE_ID_RE)


async def get_source_map(session_factory) -> dict[Messenger, str]:
    """Прочитать app_settings.source_map (мусор — {}: дефолты каналов)."""
    raw = await _read_setting(session_factory, SOURCE_MAP_KEY)
    if raw is None:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    if not isinstance(data, dict):
        logger.warning("app_settings.source_map: мусор — игнорируем: %r", raw[:64])
        return {}
    result: dict[Messenger, str] = {}
    for key, value in data.items():
        try:
            messenger = Messenger(key)
        except ValueError:
            logger.warning("source_map: неизвестный канал %r пропущен", key)
            continue
        if isinstance(value, str):
            result[messenger] = value
    return result


async def set_source_map(session_factory, mapping: dict[Messenger, str]) -> None:
    """Upsert app_settings.source_map (значения — коды записей или "")."""
    for messenger, value in mapping.items():
        if not isinstance(value, str) or not _SOURCE_ID_RE.fullmatch(value):
            raise ValueError(f"bad source for {messenger.value}: {value!r}")
    payload = {m.value: v for m, v in mapping.items()}
    await _upsert_setting(
        session_factory, SOURCE_MAP_KEY, json.dumps(payload, separators=(",", ":"))
    )


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
            .where(CrmSyncItem.status.in_([CrmSyncStatus.queued, CrmSyncStatus.retrying]))
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
                # String(512): длинная строка httpx-исключения без обрезки
                # падает на postgres, item остаётся due — hot retry loop.
                last_error=error[:512] if error is not None else None,
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
                Contact.first_name,
                Contact.last_name,
                Contact.username,
                Contact.crm_contact_id,
                Dialog.crm_deal_id,
                Dialog.crm_entity_type,
                Dialog.messenger,
                Dialog.account_id,
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
        # Адресаты уведомления «новый клиент»: ответственный, а у общего
        # номера (ответственного нет) — все активные участники линии.
        if row.b24_user_id is not None:
            notify_ids = [row.b24_user_id]
        elif row.account_id is not None:
            notify_ids = list(
                (
                    await self._session.execute(
                        select(Manager.b24_user_id)
                        .join(AccountMember, AccountMember.manager_id == Manager.id)
                        .where(
                            AccountMember.account_id == row.account_id,
                            Manager.is_active.is_(True),
                        )
                        .order_by(AccountMember.id)
                    )
                )
                .scalars()
                .all()
            )
        else:
            notify_ids = []
        # Вложения сообщения: файлы читает воркер из медиа-тома по file_path.
        att_rows = (
            await self._session.execute(
                select(
                    Attachment.file_path,
                    Attachment.file_name,
                    Attachment.mime_type,
                    Attachment.size,
                )
                .where(Attachment.message_id == message_id)
                .order_by(Attachment.id)
            )
        ).all()
        return CrmSyncData(
            message_text=row.text,
            sender_name=row.name,
            sender_phone=row.phone,
            sender_first_name=row.first_name,
            sender_last_name=row.last_name,
            sender_username=row.username,
            crm_contact_id=row.crm_contact_id,
            crm_entity_id=row.crm_deal_id,
            crm_entity_type=row.crm_entity_type,
            assigned_b24_user_id=row.b24_user_id,
            notify_user_ids=notify_ids,
            messenger=row.messenger,
            attachments=[
                AttachmentMeta(
                    file_path=a.file_path,
                    file_name=a.file_name,
                    mime_type=a.mime_type,
                    size=a.size,
                )
                for a in att_rows
            ],
        )

    async def apply_inbound_result(
        self,
        message_id: int,
        *,
        contact_id: int | None,
        crm_entity_type: str | None,
        crm_entity_id: int | None,
        timeline_comment_id: int | None,
    ) -> None:
        """Применить SyncResult к нашей БД.

        Обновляем только переданные поля (None — не трогаем), по цепочке
        Message -> Dialog -> Contact. crm_entity_type ('deal'|'lead')
        дискриминирует id в колонке dialogs.crm_deal_id.
        """
        dialog_id = await self._session.scalar(
            select(Message.dialog_id).where(Message.id == message_id)
        )
        contact_pk = (
            await self._session.scalar(select(Dialog.contact_id).where(Dialog.id == dialog_id))
            if dialog_id is not None
            else None
        )

        if timeline_comment_id is not None:
            await self._session.execute(
                update(Message)
                .where(Message.id == message_id)
                .values(timeline_comment_id=timeline_comment_id)
            )
        if dialog_id is not None and crm_entity_id is not None:
            await self._session.execute(
                update(Dialog)
                .where(Dialog.id == dialog_id)
                .values(
                    crm_deal_id=crm_entity_id,
                    crm_entity_type=crm_entity_type or "deal",
                )
            )
        if contact_pk is not None and contact_id is not None:
            await self._session.execute(
                update(Contact).where(Contact.id == contact_pk).values(crm_contact_id=contact_id)
            )
        await self._session.commit()

    async def set_timeline_comment(self, message_id: int, comment_id: int) -> None:
        await self._session.execute(
            update(Message).where(Message.id == message_id).values(timeline_comment_id=comment_id)
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

    async def get_timeline_mode(self) -> str:
        return await get_timeline_mode(self._session_factory)

    async def get_media_to_timeline(self) -> bool:
        return await get_media_to_timeline(self._session_factory)

    async def get_crm_mode(self) -> str:
        return await get_crm_mode(self._session_factory)

    async def get_source_map(self) -> dict[Messenger, str]:
        return await get_source_map(self._session_factory)

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
        crm_entity_type: str | None,
        crm_entity_id: int | None,
        timeline_comment_id: int | None,
    ) -> None:
        async with self._session_factory() as s:
            await SqlAlchemyCrmSyncRepository(s).apply_inbound_result(
                message_id,
                contact_id=contact_id,
                crm_entity_type=crm_entity_type,
                crm_entity_id=crm_entity_id,
                timeline_comment_id=timeline_comment_id,
            )

    async def set_timeline_comment(self, message_id: int, comment_id: int) -> None:
        async with self._session_factory() as s:
            await SqlAlchemyCrmSyncRepository(s).set_timeline_comment(message_id, comment_id)
