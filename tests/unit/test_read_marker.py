"""ReadMarker: ReadReceipt → Message.status='read' (исходящие диалога).

In-memory SQLite (StaticPool) по паттерну test_incoming_handler_db: marker
открывает сессии через factory, все видят одну БД.

Ключевые инварианты:
- монотонность: только sent/delivered → read (pending/error/read не трогаем);
- inbound-строки (status=delivered) исключены фильтром direction;
- TG-курсор сравнивается ЧИСЛОМ («100» <= 10 ложно, «9» <= 10 истинно —
  лексическое сравнение ловило бы наоборот);
- идемпотентность: повторная квитанция — no-op (0 строк).
"""

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.bridge.read_marker import ReadMarker
from app.messaging.types import ReadReceipt
from app.models import (
    Base,
    Contact,
    Dialog,
    Manager,
    Message,
    MessageDirection,
    MessageStatus,
    Messenger,
)


@pytest.fixture
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield SessionLocal
    await engine.dispose()


def _account(manager_id: int) -> MagicMock:
    account = MagicMock()
    account.id = manager_id  # id аккаунта-линии (см. _seed: диалог на линии 1)
    account.manager_id = manager_id
    return account


async def _seed(db, *, messenger=Messenger.tg) -> None:
    """Менеджеры 1/2, диалог чата 111 на линии 1 (менеджер 1), «чужой»
    диалог того же чата на линии 2 (messenger параметризован — квитанция
    матчится по паре messenger+chat своей линии)."""
    async with db() as s:
        s.add(Manager(id=1, name="Менеджер 1", b24_user_id=15))
        s.add(Manager(id=2, name="Менеджер 2", b24_user_id=16))
        s.add(Contact(id=10, messenger=messenger, external_user_id="999", name="Клиент"))
        s.add(
            Dialog(
                id=50,
                contact_id=10,
                messenger=messenger,
                external_chat_id="111",
                account_id=1,
                assigned_user_id=1,
            )
        )
        s.add(
            Dialog(
                id=60,
                contact_id=10,
                messenger=messenger,
                external_chat_id="111",
                account_id=2,
                assigned_user_id=2,
            )
        )
        await s.commit()


def _msg(dialog_id: int, direction, status, ext_id=None, text="m") -> Message:
    return Message(
        dialog_id=dialog_id,
        direction=direction,
        status=status,
        external_message_id=ext_id,
        text=text,
        sent_at=datetime(2026, 8, 18, tzinfo=UTC)
        if direction == MessageDirection.outbound
        else None,
    )


async def _statuses(db, dialog_id: int) -> list:
    async with db() as s:
        rows = (
            await s.execute(
                select(Message.external_message_id, Message.status)
                .where(Message.dialog_id == dialog_id)
                .order_by(Message.id)
            )
        ).all()
    return rows


@pytest.mark.asyncio
async def test_max_receipt_marks_all_sent_and_delivered(db):
    """MAX (up_to=None): все sent/delivered исходящие → read; pending/error/
    read/inbound/чужой диалог не тронуты."""
    await _seed(db, messenger=Messenger.max)
    async with db() as s:
        s.add(_msg(50, MessageDirection.outbound, MessageStatus.sent, "20"))
        s.add(_msg(50, MessageDirection.outbound, MessageStatus.delivered, "21"))
        s.add(_msg(50, MessageDirection.outbound, MessageStatus.pending))
        s.add(_msg(50, MessageDirection.outbound, MessageStatus.error, "22"))
        s.add(_msg(50, MessageDirection.outbound, MessageStatus.read, "23"))
        s.add(_msg(50, MessageDirection.inbound, MessageStatus.delivered, "24"))
        s.add(_msg(60, MessageDirection.outbound, MessageStatus.sent, "25"))
        await s.commit()

    marker = ReadMarker(db)
    count = await marker.apply(
        ReadReceipt(messenger=Messenger.max, external_chat_id="111"),  # chat тот же
        account=_account(1),
    )
    assert count == 2
    rows = dict(await _statuses(db, 50))
    assert rows["20"] == MessageStatus.read
    assert rows["21"] == MessageStatus.read
    assert rows[None] == MessageStatus.pending
    assert rows["22"] == MessageStatus.error
    assert rows["23"] == MessageStatus.read
    assert rows["24"] == MessageStatus.delivered  # inbound не тронут
    # Чужой менеджер не затронут.
    assert dict(await _statuses(db, 60))["25"] == MessageStatus.sent


@pytest.mark.asyncio
async def test_tg_receipt_numeric_cursor(db):
    """TG: числовое сравнение int(ext_id) <= max_id — «100» НЕ прочитано при
    up_to=10 (лексическое сравнение поймало бы наоборот), «9» — прочитано."""
    await _seed(db)
    async with db() as s:
        s.add(_msg(50, MessageDirection.outbound, MessageStatus.sent, "9"))
        s.add(_msg(50, MessageDirection.outbound, MessageStatus.sent, "10"))
        s.add(_msg(50, MessageDirection.outbound, MessageStatus.sent, "100"))
        # Device-outbound тоже закрывается (sent + внешний id).
        s.add(_msg(50, MessageDirection.outbound, MessageStatus.sent, "5"))
        await s.commit()

    marker = ReadMarker(db)
    count = await marker.apply(
        ReadReceipt(messenger=Messenger.tg, external_chat_id="111", up_to_external_id=10),
        account=_account(1),
    )
    assert count == 3  # 5, 9, 10
    rows = dict(await _statuses(db, 50))
    assert rows["9"] == MessageStatus.read
    assert rows["10"] == MessageStatus.read
    assert rows["100"] == MessageStatus.sent  # числовое, не лексическое


@pytest.mark.asyncio
async def test_receipt_idempotent_and_unknown_dialog(db):
    await _seed(db, messenger=Messenger.max)
    async with db() as s:
        s.add(_msg(50, MessageDirection.outbound, MessageStatus.sent, "20"))
        await s.commit()

    marker = ReadMarker(db)
    receipt = ReadReceipt(messenger=Messenger.max, external_chat_id="111")
    assert await marker.apply(receipt, account=_account(1)) == 1
    # Повторная квитанция (reconnect-шторм) — no-op.
    assert await marker.apply(receipt, account=_account(1)) == 0
    # Неизвестный чат/мессенджер — 0 без исключений.
    assert (
        await marker.apply(
            ReadReceipt(messenger=Messenger.max, external_chat_id="404"),
            account=_account(1),
        )
        == 0
    )


@pytest.mark.asyncio
async def test_wa_exact_id_receipt_marks_only_that_message(db):
    """WA (external_message_id, id нечисловой): прочитано ровно одно
    сообщение; прочие sent не тронуты (закроются своими квитанциями)."""
    await _seed(db, messenger=Messenger.wa)
    async with db() as s:
        s.add(_msg(50, MessageDirection.outbound, MessageStatus.sent, "true_7_3EB0AA"))
        s.add(_msg(50, MessageDirection.outbound, MessageStatus.sent, "true_7_3EB0BB"))
        s.add(_msg(50, MessageDirection.outbound, MessageStatus.pending))
        await s.commit()

    marker = ReadMarker(db)
    count = await marker.apply(
        ReadReceipt(
            messenger=Messenger.wa,
            external_chat_id="111",
            external_message_id="true_7_3EB0AA",
        ),
        account=_account(1),
    )
    assert count == 1
    rows = dict(await _statuses(db, 50))
    assert rows["true_7_3EB0AA"] == MessageStatus.read
    assert rows["true_7_3EB0BB"] == MessageStatus.sent
    # Идемпотентность точной квитанции.
    assert (
        await marker.apply(
            ReadReceipt(
                messenger=Messenger.wa,
                external_chat_id="111",
                external_message_id="true_7_3EB0AA",
            ),
            account=_account(1),
        )
        == 0
    )


def test_matches_numeric_not_lexical():
    """Хелпер курсора: числовое сравнение, нечисловой/пустой id — False."""
    assert ReadMarker._matches(None, None) is True  # MAX: весь диалог
    assert ReadMarker._matches("100", 10) is False
    assert ReadMarker._matches("9", 10) is True
    assert ReadMarker._matches("10", 10) is True
    assert ReadMarker._matches(None, 10) is False
    assert ReadMarker._matches("abc", 10) is False
