"""SqlAlchemyCrmSyncRepository на in-memory SQLite (по образцу outbox-repo)."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.bridge.crm_sync_repo import SqlAlchemyCrmSyncRepository
from app.models import (
    KIND_INBOUND,
    KIND_OUTBOUND,
    Base,
    Contact,
    CrmSyncItem,
    CrmSyncStatus,
    Dialog,
    Manager,
    Message,
    MessageDirection,
    Messenger,
)


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with SessionLocal() as s:
        yield s
    await engine.dispose()


async def _seed_message(session) -> int:
    """Manager + Contact + Dialog + Message; возвращает message_id."""
    session.add(Manager(id=1, name="Менеджер", b24_user_id=15))
    session.add(
        Contact(
            id=10,
            messenger=Messenger.tg,
            external_user_id="999",
            name="Иван",
            phone="+7999",
            username="ivan_p",
            first_name="Иван",
            last_name="Петров",
        )
    )
    session.add(
        Dialog(
            id=50,
            contact_id=10,
            messenger=Messenger.tg,
            external_chat_id="999",
            assigned_user_id=1,
        )
    )
    session.add(
        Message(
            id=100,
            dialog_id=50,
            direction=MessageDirection.inbound,
            text="Привет",
        )
    )
    await session.commit()
    return 100


@pytest.mark.asyncio
async def test_enqueue_and_fetch_due(session):
    repo = SqlAlchemyCrmSyncRepository(session)
    await repo.enqueue(kind=KIND_INBOUND, message_id=1)
    await session.commit()
    # second: not due yet
    item = CrmSyncItem(
        kind=KIND_OUTBOUND,
        message_id=2,
        status=CrmSyncStatus.queued,
        attempts=0,
        next_attempt_at=datetime.now(UTC) + timedelta(hours=1),
    )
    session.add(item)
    await session.commit()

    due = await repo.fetch_due(limit=10)
    assert len(due) == 1
    assert due[0].kind == KIND_INBOUND
    assert due[0].message_id == 1
    assert due[0].status == CrmSyncStatus.queued


@pytest.mark.asyncio
async def test_fetch_due_includes_retrying_excludes_terminal(session):
    """reschedule ставит retrying — fetch_due обязан их брать (урок outbox);
    done/failed — никогда."""
    session.add_all(
        [
            CrmSyncItem(
                id=1,
                kind=KIND_INBOUND,
                message_id=1,
                status=CrmSyncStatus.retrying,
                attempts=1,
                next_attempt_at=datetime.now(UTC) - timedelta(seconds=30),
            ),
            CrmSyncItem(
                id=2,
                kind=KIND_INBOUND,
                message_id=2,
                status=CrmSyncStatus.done,
                attempts=0,
                next_attempt_at=datetime.now(UTC) - timedelta(minutes=5),
            ),
            CrmSyncItem(
                id=3,
                kind=KIND_INBOUND,
                message_id=3,
                status=CrmSyncStatus.failed,
                attempts=5,
                next_attempt_at=datetime.now(UTC) - timedelta(minutes=5),
            ),
        ]
    )
    await session.commit()

    repo = SqlAlchemyCrmSyncRepository(session)
    due = await repo.fetch_due(limit=10)
    assert [i.id for i in due] == [1]


@pytest.mark.asyncio
async def test_mark_done_and_failed_and_reschedule(session):
    session.add(
        CrmSyncItem(
            id=1,
            kind=KIND_INBOUND,
            message_id=1,
            status=CrmSyncStatus.queued,
            attempts=0,
            next_attempt_at=datetime.now(UTC),
        )
    )
    await session.commit()
    repo = SqlAlchemyCrmSyncRepository(session)
    item = await session.get(CrmSyncItem, 1)

    await repo.mark_done(item)
    await session.reset()
    assert (await session.get(CrmSyncItem, 1)).status == CrmSyncStatus.done

    item = await session.get(CrmSyncItem, 1)
    await repo.reschedule(item, delay_seconds=60, error="boom")
    await session.reset()
    refreshed = await session.get(CrmSyncItem, 1)
    assert refreshed.status == CrmSyncStatus.retrying
    assert refreshed.attempts == 1
    assert refreshed.last_error == "boom"
    # SQLite возвращает naive-datetime; трактуем его как UTC.
    next_at = refreshed.next_attempt_at
    if next_at.tzinfo is None:
        next_at = next_at.replace(tzinfo=UTC)
    assert next_at > datetime.now(UTC)


@pytest.mark.asyncio
async def test_reschedule_truncates_long_error(session):
    """last_error — String(512): длинная строка httpx-исключения без обрезки
    валит UPDATE на postgres, item остаётся due — hot retry loop каждые 2с."""
    session.add(
        CrmSyncItem(
            id=1,
            kind=KIND_INBOUND,
            message_id=1,
            status=CrmSyncStatus.queued,
            attempts=0,
            next_attempt_at=datetime.now(UTC),
        )
    )
    await session.commit()

    repo = SqlAlchemyCrmSyncRepository(session)
    item = await session.get(CrmSyncItem, 1)
    await repo.reschedule(item, delay_seconds=30, error="x" * 1000)

    await session.reset()
    refreshed = await session.get(CrmSyncItem, 1)
    assert len(refreshed.last_error) == 512
    assert refreshed.last_error == "x" * 512

    item = await session.get(CrmSyncItem, 1)
    await repo.mark_failed(item, "5th fail")
    await session.reset()
    refreshed = await session.get(CrmSyncItem, 1)
    assert refreshed.status == CrmSyncStatus.failed
    assert refreshed.attempts == 2
    assert refreshed.last_error == "5th fail"


@pytest.mark.asyncio
async def test_collect_joins_message_dialog_contact_manager(session):
    await _seed_message(session)
    repo = SqlAlchemyCrmSyncRepository(session)

    data = await repo.collect(100)

    assert data is not None
    assert data.message_text == "Привет"
    assert data.sender_name == "Иван"
    assert data.sender_phone == "+7999"
    assert data.sender_first_name == "Иван"
    assert data.sender_last_name == "Петров"
    assert data.sender_username == "ivan_p"
    assert data.assigned_b24_user_id == 15
    assert data.messenger is Messenger.tg
    assert data.crm_contact_id is None
    assert data.crm_entity_id is None


@pytest.mark.asyncio
async def test_collect_missing_message_returns_none(session):
    repo = SqlAlchemyCrmSyncRepository(session)
    assert await repo.collect(404) is None


@pytest.mark.asyncio
async def test_apply_inbound_result_updates_all_three_rows(session):
    await _seed_message(session)
    repo = SqlAlchemyCrmSyncRepository(session)

    await repo.apply_inbound_result(
        100,
        contact_id=42,
        crm_entity_type="deal",
        crm_entity_id=500,
        timeline_comment_id=999,
    )

    await session.reset()
    message = await session.get(Message, 100)
    dialog = await session.get(Dialog, 50)
    contact = await session.get(Contact, 10)
    assert message.timeline_comment_id == 999
    assert dialog.crm_deal_id == 500
    assert dialog.crm_entity_type == "deal"
    assert contact.crm_contact_id == 42


@pytest.mark.asyncio
async def test_apply_inbound_result_lead_entity(session):
    """Lead-режим: id лида живёт в той же колонке crm_deal_id, тип — 'lead'."""
    await _seed_message(session)
    repo = SqlAlchemyCrmSyncRepository(session)

    await repo.apply_inbound_result(
        100,
        contact_id=None,
        crm_entity_type="lead",
        crm_entity_id=55,
        timeline_comment_id=None,
    )

    await session.reset()
    dialog = await session.get(Dialog, 50)
    contact = await session.get(Contact, 10)
    assert dialog.crm_deal_id == 55
    assert dialog.crm_entity_type == "lead"
    assert contact.crm_contact_id is None  # контакт не тронут


@pytest.mark.asyncio
async def test_apply_inbound_result_none_fields_untouched(session):
    """crm_entity_id=None не затирает существующую привязку диалога."""
    await _seed_message(session)
    dialog = await session.get(Dialog, 50)
    dialog.crm_deal_id = 77
    dialog.crm_entity_type = "deal"
    await session.commit()

    repo = SqlAlchemyCrmSyncRepository(session)
    await repo.apply_inbound_result(
        100,
        contact_id=42,
        crm_entity_type=None,
        crm_entity_id=None,
        timeline_comment_id=None,
    )

    await session.reset()
    dialog = await session.get(Dialog, 50)
    contact = await session.get(Contact, 10)
    message = await session.get(Message, 100)
    assert dialog.crm_deal_id == 77  # не затёрт
    assert contact.crm_contact_id == 42
    assert message.timeline_comment_id is None


@pytest.mark.asyncio
async def test_set_timeline_comment(session):
    await _seed_message(session)
    repo = SqlAlchemyCrmSyncRepository(session)

    await repo.set_timeline_comment(100, 555)

    await session.reset()
    message = await session.get(Message, 100)
    assert message.timeline_comment_id == 555


@pytest.mark.asyncio
async def test_collect_returns_attachments(session):
    """collect() отдаёт метаданные вложений (воркер читает файлы по file_path)."""
    message_id = await _seed_message(session)
    from app.models import Attachment, AttachmentType

    session.add(
        Attachment(
            message_id=message_id,
            type=AttachmentType.photo,
            file_path="in/abc.jpg",
            mime_type="image/jpeg",
            size=1234,
            file_name=None,
        )
    )
    await session.commit()

    data = await SqlAlchemyCrmSyncRepository(session).collect(message_id)
    assert data is not None
    assert len(data.attachments) == 1
    att = data.attachments[0]
    assert att.file_path == "in/abc.jpg"
    assert att.mime_type == "image/jpeg"
    assert att.size == 1234


@pytest.mark.asyncio
async def test_collect_without_attachments_empty_list(session):
    message_id = await _seed_message(session)
    data = await SqlAlchemyCrmSyncRepository(session).collect(message_id)
    assert data is not None
    assert data.attachments == []


@pytest.mark.asyncio
async def test_media_to_timeline_setting_roundtrip(session):
    """app_settings.media_to_timeline: off по умолчанию, upsertится."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.bridge.crm_sync_repo import (
        get_media_to_timeline,
        set_media_to_timeline,
    )

    factory = async_sessionmaker(bind=session.bind, expire_on_commit=False)

    assert await get_media_to_timeline(factory) is False
    await set_media_to_timeline(factory, True)
    assert await get_media_to_timeline(factory) is True
    await set_media_to_timeline(factory, False)
    assert await get_media_to_timeline(factory) is False


@pytest.mark.asyncio
async def test_crm_mode_setting_roundtrip(session):
    """app_settings.crm_mode: 'deal' по умолчанию, мусор — дефолт, upsert."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.bridge.crm_sync_repo import get_crm_mode, set_crm_mode

    factory = async_sessionmaker(bind=session.bind, expire_on_commit=False)

    assert await get_crm_mode(factory) == "deal"
    await set_crm_mode(factory, "lead")
    assert await get_crm_mode(factory) == "lead"
    # Мусор в таблице — fail-closed дефолт.
    from sqlalchemy import update

    from app.models import AppSetting

    async with factory() as s:
        await s.execute(
            update(AppSetting).where(AppSetting.key == "crm_mode").values(value="junk")
        )
        await s.commit()
    assert await get_crm_mode(factory) == "deal"
    with pytest.raises(ValueError):
        await set_crm_mode(factory, "client")
