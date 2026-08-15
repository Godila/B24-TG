from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.bridge.outbox_repo_sqlalchemy import SqlAlchemyOutboxRepository
from app.models import (
    Base,
    Message,
    MessageDirection,
    MessageStatus,
    OutboxItem,
    OutboxStatus,
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


@pytest.mark.asyncio
async def test_fetch_due_returns_only_due_queued(session):
    session.add(
        OutboxItem(
            id=1,
            dialog_id=10,
            tg_account_id=7,
            external_chat_id="123",
            text="hi",
            status=OutboxStatus.queued,
            attempts=0,
            next_attempt_at=datetime.now(UTC) - timedelta(minutes=1),
        )
    )
    session.add(
        OutboxItem(
            id=2,
            dialog_id=11,
            tg_account_id=7,
            external_chat_id="124",
            text="future",
            status=OutboxStatus.queued,
            attempts=0,
            next_attempt_at=datetime.now(UTC) + timedelta(hours=1),
        )
    )
    await session.commit()

    repo = SqlAlchemyOutboxRepository(session)
    due = await repo.fetch_due(limit=10)
    ids = [item.id for item in due]
    assert 1 in ids
    assert 2 not in ids


@pytest.mark.asyncio
async def test_fetch_due_excludes_non_queued(session):
    session.add(
        OutboxItem(
            id=1,
            dialog_id=10,
            tg_account_id=7,
            external_chat_id="123",
            text="done",
            status=OutboxStatus.sent,  # already sent
            attempts=0,
            next_attempt_at=datetime.now(UTC) - timedelta(minutes=5),
        )
    )
    await session.commit()

    repo = SqlAlchemyOutboxRepository(session)
    due = await repo.fetch_due(limit=10)
    assert due == []


@pytest.mark.asyncio
async def test_fetch_due_includes_retrying_after_reschedule(session):
    """Регрессия: reschedule ставит retrying; fetch_due должен повторно
    доставлять такие элементы, иначе они зависают навсегда."""
    session.add(
        OutboxItem(
            id=1,
            dialog_id=10,
            tg_account_id=7,
            external_chat_id="123",
            text="retry me",
            status=OutboxStatus.retrying,
            attempts=1,
            next_attempt_at=datetime.now(UTC) - timedelta(seconds=30),
        )
    )
    await session.commit()

    repo = SqlAlchemyOutboxRepository(session)
    due = await repo.fetch_due(limit=10)
    assert len(due) == 1
    assert due[0].id == 1


@pytest.mark.asyncio
async def test_mark_sent_sets_status(session):
    session.add(
        OutboxItem(
            id=1,
            dialog_id=10,
            tg_account_id=7,
            external_chat_id="123",
            text="hi",
            status=OutboxStatus.queued,
            attempts=0,
            next_attempt_at=datetime.now(UTC) - timedelta(minutes=1),
        )
    )
    await session.commit()

    item = OutboxItem(id=1, dialog_id=10, tg_account_id=7, external_chat_id="123")
    repo = SqlAlchemyOutboxRepository(session)
    await repo.mark_sent(item, external_message_id="999")

    await session.reset()
    refreshed = await session.get(OutboxItem, 1)
    assert refreshed.status == OutboxStatus.sent


@pytest.mark.asyncio
async def test_mark_failed_sets_status_and_error(session):
    session.add(
        OutboxItem(
            id=1,
            dialog_id=10,
            tg_account_id=7,
            external_chat_id="123",
            text="hi",
            status=OutboxStatus.queued,
            attempts=0,
            next_attempt_at=datetime.now(UTC) - timedelta(minutes=1),
        )
    )
    await session.commit()

    item = OutboxItem(id=1, dialog_id=10, tg_account_id=7, external_chat_id="123")
    repo = SqlAlchemyOutboxRepository(session)
    await repo.mark_failed(item, error="boom")

    await session.reset()
    refreshed = await session.get(OutboxItem, 1)
    assert refreshed.status == OutboxStatus.failed
    assert refreshed.last_error == "boom"


@pytest.mark.asyncio
async def test_mark_sent_updates_message(session):
    """Замыкание исходящего цикла: mark_sent должен обновлять связанный
    Message (pending -> sent, external_message_id, sent_at), а не только outbox."""
    message = Message(
        dialog_id=10,
        direction=MessageDirection.outbound,
        status=MessageStatus.pending,
        text="hi",
    )
    session.add(message)
    await session.flush()
    msg_id = message.id

    session.add(
        OutboxItem(
            dialog_id=10,
            tg_account_id=7,
            external_chat_id="123",
            text="hi",
            message_id=msg_id,
            status=OutboxStatus.queued,
            attempts=0,
            next_attempt_at=datetime.now(UTC) - timedelta(minutes=1),
        )
    )
    await session.commit()
    outbox_id = (await session.execute(select(OutboxItem.id))).scalar_one()

    item = await session.get(OutboxItem, outbox_id)
    repo = SqlAlchemyOutboxRepository(session)
    await repo.mark_sent(item, external_message_id="999")

    await session.reset()
    refreshed_outbox = await session.get(OutboxItem, outbox_id)
    refreshed_msg = await session.get(Message, msg_id)
    assert refreshed_outbox.status == OutboxStatus.sent
    assert refreshed_msg.status == MessageStatus.sent
    assert refreshed_msg.external_message_id == "999"
    assert refreshed_msg.sent_at is not None


@pytest.mark.asyncio
async def test_mark_failed_updates_message(session):
    """mark_failed должен переводить связанный Message в error."""
    message = Message(
        dialog_id=10,
        direction=MessageDirection.outbound,
        status=MessageStatus.pending,
        text="hi",
    )
    session.add(message)
    await session.flush()
    msg_id = message.id

    session.add(
        OutboxItem(
            dialog_id=10,
            tg_account_id=7,
            external_chat_id="123",
            text="hi",
            message_id=msg_id,
            status=OutboxStatus.queued,
            attempts=5,
            next_attempt_at=datetime.now(UTC) - timedelta(minutes=1),
        )
    )
    await session.commit()
    outbox_id = (await session.execute(select(OutboxItem.id))).scalar_one()

    item = await session.get(OutboxItem, outbox_id)
    repo = SqlAlchemyOutboxRepository(session)
    await repo.mark_failed(item, error="boom")

    await session.reset()
    refreshed_outbox = await session.get(OutboxItem, outbox_id)
    refreshed_msg = await session.get(Message, msg_id)
    assert refreshed_outbox.status == OutboxStatus.failed
    assert refreshed_outbox.last_error == "boom"
    assert refreshed_msg.status == MessageStatus.error


@pytest.mark.asyncio
async def test_reschedule_deferral_no_attempt_increment(session):
    """Безобидные отклонения (throttle/no_provider) не расходуют попытки:
    count_attempt=False оставляет attempts; True (дефолт) — инкрементирует."""
    session.add(
        OutboxItem(
            dialog_id=10,
            tg_account_id=7,
            external_chat_id="123",
            text="hi",
            status=OutboxStatus.queued,
            attempts=2,
            next_attempt_at=datetime.now(UTC) - timedelta(minutes=1),
        )
    )
    await session.commit()
    outbox_id = (await session.execute(select(OutboxItem.id))).scalar_one()

    item = await session.get(OutboxItem, outbox_id)
    repo = SqlAlchemyOutboxRepository(session)
    await repo.reschedule(
        item, delay_seconds=10, error="throttled", count_attempt=False
    )

    await session.reset()
    refreshed = await session.get(OutboxItem, outbox_id)
    assert refreshed.status == OutboxStatus.retrying
    assert refreshed.attempts == 2  # не инкрементирован
    assert refreshed.last_error == "throttled"

    await repo.reschedule(item, delay_seconds=30, error="boom")  # count_attempt=True

    await session.reset()
    refreshed = await session.get(OutboxItem, outbox_id)
    assert refreshed.attempts == 3  # инкрементирован


@pytest.mark.asyncio
async def test_reschedule_increments_attempts_and_sets_retrying(session):
    session.add(
        OutboxItem(
            id=1,
            dialog_id=10,
            tg_account_id=7,
            external_chat_id="123",
            text="hi",
            status=OutboxStatus.queued,
            attempts=0,
            next_attempt_at=datetime.now(UTC) - timedelta(minutes=1),
        )
    )
    await session.commit()

    item = await session.get(OutboxItem, 1)
    repo = SqlAlchemyOutboxRepository(session)
    await repo.reschedule(item, delay_seconds=60, error="retry")

    await session.reset()
    refreshed = await session.get(OutboxItem, 1)
    assert refreshed.status == OutboxStatus.retrying
    assert refreshed.attempts == 1
    assert refreshed.last_error == "retry"
