"""Юнит-тесты WaOnboardingChannel: старт сессии с прокси, поллинг QR/статуса,
сохранение кредов линии, дедлайн → expired + cleanup, cancel.

FakeClient реализует используемое подмножество REST (сети нет); БД —
in-memory SQLite (StaticPool).
"""

import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.messaging.whatsapp.api import WaError
from app.models import Base, Manager, Messenger, TgAccount, TgAccountStatus
from app.onboarding.types import OnboardingStatus
from app.onboarding.wa_channel import WaOnboardingChannel


class FakeClient:
    """Скриптованный REST: сессия оживает к N-му get_session."""

    def __init__(self, ready_on_poll=1, phone="79160001122", push_name="Джордж"):
        self.created = []
        self.started = []
        self.logged_out = []
        self.deleted = []
        self.calls = 0
        self._ready_on_poll = ready_on_poll
        self._phone = phone
        self._push = push_name

    async def create_session(self, name, *, proxy_url=None):
        self.created.append((name, proxy_url))
        return {"id": f"sess-{len(self.created)}", "status": "created"}

    async def start_session(self, sid):
        self.started.append(sid)

    async def get_session(self, sid):
        self.calls += 1
        if self.calls >= self._ready_on_poll:
            return {
                "id": sid,
                "status": "ready",
                "phone": self._phone,
                "pushName": self._push,
            }
        return {"id": sid, "status": "initializing"}

    async def session_qr(self, sid):
        if self.calls < self._ready_on_poll:
            return {"qrCode": "data:image/png;base64,QR==", "status": "qr_ready"}
        raise WaError("openwa 400 /qr: already authenticated")

    async def logout_session(self, sid):
        self.logged_out.append(sid)

    async def delete_session(self, sid):
        self.deleted.append(sid)

    async def aclose(self):
        pass


@pytest.fixture
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        s.add(Manager(id=1, name="Менеджер 1", b24_user_id=15))
        s.add(
            TgAccount(
                id=5,
                messenger=Messenger.wa,
                phone="WA-LINE-abc",
                status=TgAccountStatus.offline,
                manager_id=1,
            )
        )
        await s.commit()
    yield factory
    await engine.dispose()


def make_channel(db, client):
    channel = WaOnboardingChannel(session_factory=db, client_factory=lambda: client)
    return channel


def account_stub() -> TgAccount:
    return TgAccount(
        id=5, messenger=Messenger.wa, phone="WA-LINE-abc",
        status=TgAccountStatus.offline, manager_id=1,
    )


async def wait_status(channel, wanted, timeout=2.0):
    for _ in range(int(timeout / 0.02)):
        view = await channel.login_view(5)
        if view is not None and view.status is wanted:
            return view
        await asyncio.sleep(0.02)
    raise AssertionError(f"статус {wanted} не наступил: {view}")


async def test_full_flow_creates_saves_and_activates(db):
    client = FakeClient(ready_on_poll=2)
    channel = make_channel(db, client)
    result = await channel.start(account_stub())
    assert result["status"] == OnboardingStatus.waiting.value
    assert client.created == [("chatmost-5", "socks5://xray-client:10808")]
    assert client.started == ["sess-1"]

    view = await wait_status(channel, OnboardingStatus.authorized)
    assert view.error is None

    async with db() as s:
        row = (await s.execute(select(TgAccount).where(TgAccount.id == 5))).scalar_one()
        assert row.wa_session_id == "sess-1"
        assert row.phone == "79160001122"
        assert row.display_name == "Джордж"
        assert row.status is TgAccountStatus.active
    assert client.logged_out == []  # успех — сессия жива


async def test_failed_session_maps_to_error(db):
    client = FakeClient()
    client.get_session = lambda sid: _async_return(
        {"id": sid, "status": "failed", "lastError": "TOS_BLOCK"}
    )
    channel = make_channel(db, client)
    await channel.start(account_stub())
    view = await wait_status(channel, OnboardingStatus.error)
    assert "TOS_BLOCK" in view.error


async def _async_return(value):
    await asyncio.sleep(0)
    return value


async def test_start_active_without_force_is_noop(db):
    client = FakeClient()
    channel = make_channel(db, client)
    active = TgAccount(
        id=5, messenger=Messenger.wa, phone="+70001",
        status=TgAccountStatus.active, manager_id=1,
    )
    result = await channel.start(active)
    assert result == {"status": "already_active"}
    assert client.created == []


async def test_cancel_cleans_up_session(db):
    client = FakeClient(ready_on_poll=10**9)  # никогда не ready
    channel = make_channel(db, client)
    await channel.start(account_stub())
    await asyncio.sleep(0.05)
    await channel.cancel(5)
    assert client.logged_out == ["sess-1"]
    assert client.deleted == ["sess-1"]
    assert await channel.login_view(5) is None
