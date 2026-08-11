from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.bridge.outbox_repo_worker import WorkerOutboxRepository
from app.models import Base, OutboxItem, OutboxStatus


@pytest.fixture
async def session_factory():
    """Общий in-memory движок; создаём таблицы один раз.

    Важно: фабрику сессий (а не одну сессию) отдаём в WorkerOutboxRepository,
    чтобы каждый вызов метода открывал свою сессию — как в production.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_fetch_due_returns_due_item_via_fresh_session(session_factory):
    # Сидим одну запись через отдельную сессию (имитируем вставку из route).
    async with session_factory() as s:
        s.add(
            OutboxItem(
                dialog_id=10,
                tg_account_id=7,
                external_chat_id="123",
                text="hi",
                status=OutboxStatus.queued,
                attempts=0,
                next_attempt_at=datetime.now(UTC) - timedelta(minutes=1),
            )
        )
        await s.commit()

    repo = WorkerOutboxRepository(session_factory)
    due = await repo.fetch_due(limit=10)
    assert len(due) == 1
    assert due[0].external_chat_id == "123"


@pytest.mark.asyncio
async def test_mark_sent_across_two_fresh_sessions(session_factory):
    """Регрессия: две операции подряд не должны конфликтовать.

    fetch_due открывает (и закрывает) одну сессию, mark_sent — другую.
    Результат первой (OutboxItem из закрытой сессии) передаётся во вторую
    по id — worker-адаптер не должен держать долгоживущую сессию."""
    async with session_factory() as s:
        s.add(
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
        await s.commit()

    repo = WorkerOutboxRepository(session_factory)
    due = await repo.fetch_due(limit=10)
    assert len(due) == 1
    item = due[0]

    await repo.mark_sent(item, external_message_id=999)

    # Проверяем в новой сессии — статус сменился на sent.
    async with session_factory() as s:
        refreshed = await s.get(OutboxItem, 1)
        assert refreshed.status == OutboxStatus.sent


@pytest.mark.asyncio
async def test_enqueue_then_fetch_roundtrip(session_factory):
    repo = WorkerOutboxRepository(session_factory)
    await repo.enqueue(
        dialog_id=10,
        tg_account_id=7,
        external_chat_id="999",
        text="roundtrip",
        is_initiation=False,
    )

    due = await repo.fetch_due(limit=10)
    assert len(due) == 1
    assert due[0].text == "roundtrip"
