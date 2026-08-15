"""DB-тесты IncomingHandler: скоп диалогов по менеджеру (мультиаккаунт).

В приватных TG-чатах ``external_chat_id`` == tg-id клиента и СОВПАДАЕТ у всех
менеджеров, поэтому upsert диалога обязан искать пару
``(external_chat_id, assigned_user_id)``, а не только chat_id. Здесь реальная
in-memory SQLite (StaticPool): handler открывает сессии через
``db_session_factory`` (session-per-call), все сессии видят одну БД.

План 006: CRM-полей в persist больше нет (contact_id/deal_id/
timeline_comment_id пишет CrmSyncWorker из очереди) — handler только
сохраняет сообщение и ставит crm_sync-задачу.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import Select, select, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.bridge.incoming_handler import IncomingHandler
from app.messaging.types import ContentType, IncomingMessage
from app.models import (
    Base,
    Contact,
    Dialog,
    Manager,
    Message,
    Messenger,
    TgAccount,
)

# До-миграционная схема dialogs (вывод create_all до плана 004): без
# unique-констрейнта на (external_chat_id, assigned_user_id). Нужна, чтобы
# засеять legacy-дубли, оставшиеся от гонки старого upsert.
_LEGACY_DIALOGS_DDL = """
CREATE TABLE dialogs (
    id INTEGER NOT NULL PRIMARY KEY,
    contact_id INTEGER NOT NULL REFERENCES contacts (id),
    messenger VARCHAR(3) NOT NULL,
    external_chat_id VARCHAR(128) NOT NULL,
    crm_deal_id INTEGER,
    crm_entity_type VARCHAR(32),
    assigned_user_id INTEGER,
    title VARCHAR(255),
    status VARCHAR(8) NOT NULL,
    last_msg_at DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL
)
"""


@pytest.fixture
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    SessionLocal = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    yield SessionLocal
    await engine.dispose()


class _StaleDialogSelectSession(AsyncSession):
    """Имитация гонки вставки: первый SELECT диалога «не видит» строку,
    вставленную параллельной задачей (протухший read), и последующий INSERT
    падает с IntegrityError по unique-констрейнту."""

    _stale_once = False

    async def execute(self, statement, *args, **kwargs):
        if (
            not self._stale_once
            and isinstance(statement, Select)
            and statement.column_descriptions[0]["entity"] is Dialog
        ):
            self._stale_once = True
            statement = select(Dialog).where(Dialog.id == -1)
        return await super().execute(statement, *args, **kwargs)


def _make_msg(**kw):
    defaults = {
        "messenger": Messenger.tg,
        "external_chat_id": "111",
        "sender_external_id": "999",
        "sender_name": "Клиент",
        "sender_phone": None,
        "sender_username": None,
        "content_type": ContentType.text,
        "text": "Привет",
        "external_message_id": "1",
        "is_reply": False,
    }
    defaults.update(kw)
    return IncomingMessage(**defaults)


def _make_account(manager_id: int, b24_user_id: int = 15) -> MagicMock:
    account = MagicMock()
    account.manager_id = manager_id
    account.manager.b24_user_id = b24_user_id
    return account


def _make_handler(SessionLocal, *, db_factory=None):
    """Handler с замоканным crm_sync_enqueue; возвращает (handler, enqueue)."""
    enqueue = AsyncMock()
    return (
        IncomingHandler(
            crm_sync_enqueue=enqueue,
            db_session_factory=db_factory or SessionLocal,
        ),
        enqueue,
    )


async def _seed_two_managers(SessionLocal) -> None:
    async with SessionLocal() as s:
        s.add(Manager(id=1, name="Менеджер 1", b24_user_id=15))
        s.add(Manager(id=2, name="Менеджер 2", b24_user_id=16))
        s.add(TgAccount(id=1, phone="+79990000001", session_path="/tmp/s1", manager_id=1))
        s.add(TgAccount(id=2, phone="+79990000002", session_path="/tmp/s2", manager_id=2))
        await s.commit()


async def _all_dialogs(SessionLocal) -> list[Dialog]:
    async with SessionLocal() as s:
        return list(
            (await s.execute(select(Dialog).order_by(Dialog.id))).scalars()
        )


async def _all_messages(SessionLocal) -> list[Message]:
    async with SessionLocal() as s:
        return list(
            (await s.execute(select(Message).order_by(Message.id))).scalars()
        )


@pytest.mark.asyncio
async def test_same_client_two_managers_two_dialogs(db):
    """Один и тот же клиент пишет двум менеджерам → ДВА диалога, по одному
    на менеджера (в приватных чатах chat_id у обоих сообщений одинаковый)."""
    await _seed_two_managers(db)

    handler, enqueue = _make_handler(db)
    await handler.handle(
        _make_msg(external_message_id="1"), account=_make_account(manager_id=1)
    )
    await handler.handle(
        _make_msg(external_message_id="2"), account=_make_account(manager_id=2)
    )

    dialogs = await _all_dialogs(db)
    assert len(dialogs) == 2
    assert {d.assigned_user_id for d in dialogs} == {1, 2}
    # Сообщения не потерялись: по одному в каждом диалоге.
    messages = await _all_messages(db)
    assert len(messages) == 2
    assert {m.dialog_id for m in messages} == {d.id for d in dialogs}
    # Каждое сообщение получило свою CRM-задачу с реальным message_id.
    assert enqueue.await_count == 2
    enqueued_ids = {c.kwargs["message_id"] for c in enqueue.await_args_list}
    assert enqueued_ids == {m.id for m in messages}
    assert all(c.kwargs["kind"] == "inbound" for c in enqueue.await_args_list)


@pytest.mark.asyncio
async def test_concurrent_duplicate_insert_resolved(db):
    """Legacy-дубли (chat_id, manager) не роняют обработку MultipleResultsFound'ом:
    переиспользуется старейший диалог пары, новый не создаётся."""
    await _seed_two_managers(db)
    async with db() as s:
        # Пересоздаём dialogs по до-миграционной схеме (без unique), иначе
        # засеять дубли нельзя.
        await s.execute(text("DROP TABLE dialogs"))
        await s.execute(text(_LEGACY_DIALOGS_DDL))
        s.add(Contact(id=10, messenger=Messenger.tg, external_user_id="999", name="Клиент"))
        s.add(
            Dialog(
                id=101,
                contact_id=10,
                messenger=Messenger.tg,
                external_chat_id="111",
                assigned_user_id=1,
            )
        )
        s.add(
            Dialog(
                id=102,
                contact_id=10,
                messenger=Messenger.tg,
                external_chat_id="111",
                assigned_user_id=1,
            )
        )
        await s.commit()

    handler, enqueue = _make_handler(db)
    # Не должно бросить MultipleResultsFound и не должно создать 3-й диалог.
    await handler.handle(
        _make_msg(external_message_id="1"), account=_make_account(manager_id=1)
    )

    dialogs = await _all_dialogs(db)
    assert len(dialogs) == 2
    messages = await _all_messages(db)
    assert len(messages) == 1
    assert messages[0].dialog_id == 101  # переиспользован старейший
    enqueue.assert_awaited_once_with(
        kind="inbound", message_id=messages[0].id
    )


@pytest.mark.asyncio
async def test_existing_dialog_of_other_manager_not_reused(db):
    """Диалог клиента у менеджера 1 нетронут, когда тот же клиент пишет
    менеджеру 2: создаётся НОВЫЙ диалог; CRM-поля старого не затёрты (их
    теперь пишет только CrmSyncWorker, не persist)."""
    await _seed_two_managers(db)
    async with db() as s:
        s.add(Contact(id=10, messenger=Messenger.tg, external_user_id="999", name="Клиент"))
        s.add(
            Dialog(
                id=50,
                contact_id=10,
                messenger=Messenger.tg,
                external_chat_id="111",
                assigned_user_id=1,
                crm_deal_id=42,
                crm_entity_type="deal",
            )
        )
        await s.commit()

    handler, enqueue = _make_handler(db)
    await handler.handle(
        _make_msg(external_message_id="5"), account=_make_account(manager_id=2)
    )

    dialogs = await _all_dialogs(db)
    assert len(dialogs) == 2
    by_manager = {d.assigned_user_id: d for d in dialogs}
    assert by_manager[1].crm_deal_id == 42  # диалог менеджера 1 нетронут
    # Новый диалог без CRM-привязки — её проставит воркер из SyncResult.
    assert by_manager[2].crm_deal_id is None
    assert by_manager[2].id != 50
    enqueue.assert_awaited_once()


@pytest.mark.asyncio
async def test_integrity_error_race_reuses_existing_dialog(db):
    """Гонка вставки: SELECT диалога не увидел параллельную вставку →
    INSERT падает IntegrityError → rollback → берём существующий диалог;
    контакт после rollback остаётся консистентным."""
    await _seed_two_managers(db)
    async with db() as s:
        s.add(Contact(id=10, messenger=Messenger.tg, external_user_id="999", name="Клиент"))
        s.add(
            Dialog(
                id=50,
                contact_id=10,
                messenger=Messenger.tg,
                external_chat_id="111",
                assigned_user_id=1,
                crm_deal_id=42,
            )
        )
        await s.commit()

    def stale_factory():
        return _StaleDialogSelectSession(db.kw["bind"], expire_on_commit=False)

    handler, enqueue = _make_handler(db, db_factory=stale_factory)
    await handler.handle(
        _make_msg(external_message_id="9"), account=_make_account(manager_id=1)
    )

    dialogs = await _all_dialogs(db)
    assert len(dialogs) == 1
    assert dialogs[0].id == 50  # переиспользован вставленный «конкурентом»
    assert dialogs[0].crm_deal_id == 42  # не затёрт
    messages = await _all_messages(db)
    assert len(messages) == 1
    assert messages[0].dialog_id == 50
    # CRM-задача поставлена с id реально сохранённого сообщения.
    enqueue.assert_awaited_once_with(kind="inbound", message_id=messages[0].id)
    # Rollback откатил и контактную часть txn — она восстановлена (без CRM-полей).
    async with db() as s:
        contact = await s.get(Contact, 10)
        assert contact is not None
        assert contact.crm_contact_id is None
