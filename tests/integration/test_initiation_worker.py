"""InitiationWorker: резолв → Contact/Dialog/Message/outbox одной транзакцией."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.bridge.initiation_worker import InitiationWorker
from app.messaging.resolve import ParsedDest, ResolvedPeer
from app.models import (
    Contact,
    Dialog,
    Initiation,
    InitiationStatus,
    Manager,
    Message,
    MessageDirection,
    Messenger,
    OutboxItem,
    TgAccount,
)


class _FakeProvider:
    def __init__(self, peer: ResolvedPeer | None = None, error: Exception | None = None):
        self._peer = peer
        self._error = error
        self.calls: list[ParsedDest] = []

    def is_connected(self) -> bool:
        return True

    async def resolve_peer(self, dest: ParsedDest):
        self.calls.append(dest)
        if self._error is not None:
            raise self._error
        return self._peer


class _FakeSM:
    def __init__(self, provider):
        self._provider = provider

    def get(self, account_id: int):
        return self._provider


_PEER = ResolvedPeer(
    external_user_id="888",
    external_chat_id="888",
    name="Пётр",
    username="petr",
    phone="+79990000000",
)


@pytest.fixture
async def env():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        from app.models import Base

        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as s:
        s.add(Manager(id=1, name="Иван", b24_user_id=15, is_active=True))
        s.add(
            TgAccount(
                id=7, messenger=Messenger.tg, phone="+79991234567",
                session_path="/tmp/s", manager_id=1,
            )
        )
        await s.commit()
    yield session_factory
    await engine.dispose()


async def _seed_initiation(sf, **kw) -> int:
    defaults = {
        "account_id": 7,
        "messenger": Messenger.tg,
        "author_manager_id": 1,
        "author_b24_user_id": 15,
        "entity_type": "deal",
        "entity_id": 42,
        "dest_kind": "phone",
        "dest_value": "+79990000000",
        "text": "Здравствуйте!",
        "status": InitiationStatus.pending,
        "attempts": 0,
        "next_attempt_at": datetime.now(UTC),
    }
    defaults.update(kw)
    async with sf() as s:
        cmd = Initiation(**defaults)
        s.add(cmd)
        await s.commit()
        return cmd.id


async def _worker(sf, provider) -> InitiationWorker:
    return InitiationWorker(sm=_FakeSM(provider), session_factory=sf)


@pytest.mark.asyncio
async def test_happy_path_creates_contact_dialog_message_outbox(env):
    cmd_id = await _seed_initiation(env)
    worker = await _worker(env, _FakeProvider(_PEER))
    await worker._handle(cmd_id)

    async with env() as s:
        cmd = await s.get(Initiation, cmd_id)
        assert cmd.status is InitiationStatus.linked
        assert cmd.dialog_id is not None
        contact = (
            await s.execute(select(Contact).where(Contact.external_user_id == "888"))
        ).scalar_one()
        assert contact.name == "Пётр"
        assert contact.username == "petr"
        dialog = await s.get(Dialog, cmd.dialog_id)
        assert dialog.crm_deal_id == 42 and dialog.crm_entity_type == "deal"
        assert dialog.assigned_user_id == 1  # личный аккаунт → владелец
        assert dialog.last_msg_at is not None
        message = (
            await s.execute(select(Message).where(Message.dialog_id == dialog.id))
        ).scalar_one()
        assert message.direction is MessageDirection.outbound
        assert message.author_user_id == 15
        item = (
            await s.execute(select(OutboxItem).where(OutboxItem.message_id == message.id))
        ).scalar_one()
        assert item.is_initiation is True  # анти-бан throttler
        assert item.external_chat_id == "888"


@pytest.mark.asyncio
async def test_contact_card_binds_contact_not_dialog(env):
    """Карточка контакта: Contact.crm_contact_id, Dialog БЕЗ crm-полей
    (sync.py трактует неизвестный crm_entity_type как сделку)."""
    cmd_id = await _seed_initiation(env, entity_type="contact", entity_id=33)
    worker = await _worker(env, _FakeProvider(_PEER))
    await worker._handle(cmd_id)

    async with env() as s:
        cmd = await s.get(Initiation, cmd_id)
        dialog = await s.get(Dialog, cmd.dialog_id)
        assert dialog.crm_deal_id is None and dialog.crm_entity_type is None
        contact = await s.get(Contact, dialog.contact_id)
        assert contact.crm_contact_id == 33


@pytest.mark.asyncio
async def test_existing_dialog_reused_and_rebound_to_manager_card(env):
    """Диалог уже есть (клиент писал на другую карточку) — reuse + rebind:
    интент менеджера авторитетен, диалог виден в карточке инициирования."""
    async with env() as s:
        old_contact = Contact(
            messenger=Messenger.tg, external_user_id="888", name="Пётр"
        )
        s.add(old_contact)
        await s.flush()
        s.add(
            Dialog(
                contact_id=old_contact.id,
                messenger=Messenger.tg,
                external_chat_id="888",
                account_id=7,
                crm_deal_id=100,
                crm_entity_type="deal",
            )
        )
        await s.commit()
    cmd_id = await _seed_initiation(env, entity_id=42)
    worker = await _worker(env, _FakeProvider(_PEER))
    await worker._handle(cmd_id)

    async with env() as s:
        dialogs = (
            (await s.execute(select(Dialog))).scalars().all()
        )
        assert len(dialogs) == 1  # дубля нет
        assert dialogs[0].crm_deal_id == 42  # rebind на карточку менеджера
        cmd = await s.get(Initiation, cmd_id)
        assert cmd.dialog_id == dialogs[0].id


@pytest.mark.asyncio
async def test_not_found_is_terminal_failure(env):
    cmd_id = await _seed_initiation(env)
    worker = await _worker(env, _FakeProvider(None))
    await worker._handle(cmd_id)
    async with env() as s:
        cmd = await s.get(Initiation, cmd_id)
        assert cmd.status is InitiationStatus.failed
        assert "Не найден" in cmd.last_error
        assert cmd.dialog_id is None


@pytest.mark.asyncio
async def test_not_supported_is_terminal_failure(env):
    cmd_id = await _seed_initiation(env)
    worker = await _worker(env, _FakeProvider(error=NotImplementedError("nope")))
    await worker._handle(cmd_id)
    async with env() as s:
        cmd = await s.get(Initiation, cmd_id)
        assert cmd.status is InitiationStatus.failed
        assert "не поддерживает" in cmd.last_error


@pytest.mark.asyncio
async def test_resolve_error_burns_attempt_with_backoff(env):
    cmd_id = await _seed_initiation(env)
    worker = await _worker(env, _FakeProvider(error=ConnectionError("net")))
    await worker._handle(cmd_id)
    async with env() as s:
        cmd = await s.get(Initiation, cmd_id)
        assert cmd.status is InitiationStatus.pending
        assert cmd.attempts == 1  # попытка сгорела, backoff назначен


@pytest.mark.asyncio
async def test_resolve_error_exhausted_fails(env):
    cmd_id = await _seed_initiation(env, attempts=2)
    worker = await _worker(env, _FakeProvider(error=ConnectionError("net")))
    await worker._handle(cmd_id)
    async with env() as s:
        cmd = await s.get(Initiation, cmd_id)
        assert cmd.status is InitiationStatus.failed


@pytest.mark.asyncio
async def test_no_provider_reschedules_then_fails_after_deadline(env):
    class _NoSM:
        def get(self, account_id):
            return None

    worker = InitiationWorker(sm=_NoSM(), session_factory=env)
    cmd_id = await _seed_initiation(env)
    await worker._handle(cmd_id)
    async with env() as s:
        cmd = await s.get(Initiation, cmd_id)
        assert cmd.status is InitiationStatus.pending
        assert cmd.attempts == 0  # попытка не сгорела
    # Ключ старше дедлайна → честный failed.
    cmd_id2 = await _seed_initiation(
        env,
        dest_value="+79995550000",  # другой dest: uq_initiations_active
        created_at=datetime.now(UTC) - timedelta(minutes=5),
    )
    await worker._handle(cmd_id2)
    async with env() as s:
        cmd = await s.get(Initiation, cmd_id2)
        assert cmd.status is InitiationStatus.failed
        assert "офлайн" in cmd.last_error
