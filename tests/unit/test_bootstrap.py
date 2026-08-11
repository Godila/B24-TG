from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.bridge.bootstrap import (
    forward_incoming,
    load_active_accounts,
    register_accounts,
)
from app.messaging.types import ContentType, IncomingMessage
from app.models import Base, Manager, ManagerRole, TgAccount, TgAccountStatus


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _seed_accounts(session_factory) -> None:
    """Два менеджера + два аккаунта: один active, другой offline."""
    async with session_factory() as s:
        m1 = Manager(name="Alice", b24_user_id=11, role=ManagerRole.manager)
        m2 = Manager(name="Bob", b24_user_id=12, role=ManagerRole.manager)
        s.add_all([m1, m2])
        await s.flush()
        s.add_all(
            [
                TgAccount(
                    phone="+70001",
                    session_path="/tmp/a1",
                    status=TgAccountStatus.active,
                    manager_id=m1.id,
                ),
                TgAccount(
                    phone="+70002",
                    session_path="/tmp/a2",
                    status=TgAccountStatus.offline,
                    manager_id=m2.id,
                ),
            ]
        )
        await s.commit()


@pytest.mark.asyncio
async def test_load_active_accounts_returns_only_active(session_factory):
    await _seed_accounts(session_factory)

    accounts = await load_active_accounts(session_factory)

    assert len(accounts) == 1
    only = accounts[0]
    assert only.status == TgAccountStatus.active
    # Eager-load критичен: после закрытия стартовой сессии обращение к
    # .manager не должно бросать DetachedInstanceError.
    assert only.manager is not None
    assert only.manager.b24_user_id == 11


@pytest.mark.asyncio
async def test_register_accounts_skips_failed():
    """Один аккаунт падает при register — другие регистрируются, без исключения."""
    sm = AsyncMock()

    ok_account = MagicMock(id=1, phone="+1")
    bad_account = MagicMock(id=2, phone="+2")

    async def fake_register(account):
        if account.id == 2:
            raise RuntimeError("connect failed")

    sm.register = AsyncMock(side_effect=fake_register)

    registered = await register_accounts(sm, [ok_account, bad_account])

    assert set(registered.keys()) == {1}
    assert registered[1] is ok_account
    assert sm.register.await_count == 2


@pytest.mark.asyncio
async def test_forward_incoming_overwrites_account_id():
    """TG-провайдер шлёт msg с account_id=0 — цикл обязан подставить реальный id."""

    real_msg = IncomingMessage(
        account_id=0,  # HARDCODED в TelegramProvider
        external_chat_id="42",
        sender_tg_id=999,
        sender_name=None,
        sender_phone=None,
        sender_username=None,
        content_type=ContentType.text,
        text="hi",
    )

    class FakeProvider:
        async def incoming_stream(self):
            yield real_msg  # конечный поток — тест не зависнет

    handler = AsyncMock()
    account = MagicMock(id=77)

    await forward_incoming(FakeProvider(), account, handler)

    handler.handle.assert_awaited_once()
    # Аккаунт передан как keyword-only.
    assert handler.handle.call_args.kwargs["account"] is account
    # КРИТИЧНО: msg.account_id перезаписан на реальный.
    assert real_msg.account_id == 77


@pytest.mark.asyncio
async def test_forward_incoming_survives_handler_error():
    """Ошибка в handler для одного сообщения не убивает подписку."""

    msg1 = IncomingMessage(
        account_id=0,
        external_chat_id="1",
        sender_tg_id=1,
        sender_name=None,
        sender_phone=None,
        sender_username=None,
        content_type=ContentType.text,
        text="boom",
    )
    msg2 = IncomingMessage(
        account_id=0,
        external_chat_id="2",
        sender_tg_id=2,
        sender_name=None,
        sender_phone=None,
        sender_username=None,
        content_type=ContentType.text,
        text="ok",
    )

    class FakeProvider:
        async def incoming_stream(self):
            yield msg1
            yield msg2

    handler = AsyncMock()
    handler.handle = AsyncMock(
        side_effect=[RuntimeError("handler crashed"), None]
    )
    account = MagicMock(id=5)

    await forward_incoming(FakeProvider(), account, handler)

    assert handler.handle.await_count == 2
    assert msg1.account_id == 5
    assert msg2.account_id == 5
