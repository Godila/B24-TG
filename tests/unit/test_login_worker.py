"""LoginCommandWorker: машина состояний QR-логина на мок-клиенте (без сети)."""

import asyncio

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from telethon.errors import SessionPasswordNeededError

from app.bridge.login_worker import LoginCommandWorker
from app.models import (
    Base,
    LoginCommand,
    LoginCommandKind,
    LoginCommandStatus,
    Manager,
    Messenger,
    TgAccount,
    TgAccountStatus,
)


class FakeQr:
    def __init__(self, outcomes):
        self.url = "tg://login?token=1"
        self._outcomes = list(outcomes)
        self.recreate_calls = 0

    async def wait(self):
        if not self._outcomes:
            await asyncio.sleep(60)  # «вечное» ожидание для тестов отмены
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def recreate(self):
        self.recreate_calls += 1
        self.url = f"tg://login?token={self.recreate_calls + 1}"


class FakeClient:
    def __init__(self, *, authorized=False, qr=None, user=None, password_ok=True):
        self._authorized = authorized
        self._qr = qr
        self._user = user or type("U", (), {"phone": "+79990000001", "first_name": "Иван", "last_name": None})()
        self._password_ok = password_ok
        self.connected = False
        self.logged_out = False

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.connected = False

    async def is_user_authorized(self):
        return self._authorized

    async def get_me(self):
        return self._user

    async def qr_login(self):
        return self._qr

    async def sign_in(self, password=None):
        if not self._password_ok:
            raise RuntimeError("PasswordHashInvalid")
        return self._user

    async def log_out(self):
        self.logged_out = True


class FakeSm:
    def __init__(self):
        self.providers: dict[int, object] = {}

    def get(self, account_id):
        return self.providers.get(account_id)


class FakeAccountSync:
    def __init__(self):
        self.forced = []

    async def force_unregister(self, account_id, *, reason):
        self.forced.append((account_id, reason))


@pytest.fixture
async def db(tmp_path):
    #: ФАЙЛОВЫЙ sqlite + два движка: воркер и «параллельный писатель»
    #: (feeder пароля/отмены) работают в РАЗНЫХ соединениях — StaticPool
    #: с одним соединением interleaved-транзакциями гонит тесты.
    path = tmp_path / "login_commands.db"
    url = f"sqlite+aiosqlite:///{path.as_posix()}"
    engine = create_async_engine(url)
    side = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    SideLocal = async_sessionmaker(side, class_=AsyncSession, expire_on_commit=False)
    async with SessionLocal() as s:
        s.add(Manager(id=1, name="Иван", b24_user_id=15, is_active=True))
        s.add(
            TgAccount(
                id=7, messenger=Messenger.tg, phone="TG-mgr1",
                status=TgAccountStatus.offline, manager_id=1,
            )
        )
        await s.commit()
    SessionLocal.side = SideLocal  # для фоновых писателей в тестах
    yield SessionLocal
    await engine.dispose()
    await side.dispose()


def _worker(db, client, *, account_id=7):
    sm = FakeSm()
    sync = FakeAccountSync()
    worker = LoginCommandWorker(
        sm=sm,
        account_sync=sync,
        session_factory=db,
        client_factory=lambda aid: client,
        control_poll_sec=0.01,
        password_timeout_sec=1.0,
        qr_iterations=3,
    )
    worker._test_sm = sm
    worker._test_sync = sync
    return worker


async def _make_cmd(db, *, kind=LoginCommandKind.qr_login, account_id=7) -> int:
    async with db() as s:
        cmd = LoginCommand(
            manager_id=1,
            account_id=account_id,
            messenger=Messenger.tg,
            kind=kind,
            status=LoginCommandStatus.pending,
        )
        s.add(cmd)
        await s.commit()
        return cmd.id


async def _get_cmd(db, cmd_id) -> LoginCommand:
    async with db() as s:
        cmd = await s.get(LoginCommand, cmd_id)
        return cmd


async def _get_account(db) -> TgAccount:
    async with db() as s:
        return await s.get(TgAccount, 7)


async def test_qr_success_flow(db):
    user = type("U", (), {"phone": "+79990000001", "first_name": "Иван", "last_name": None})()
    qr = FakeQr(outcomes=[user])  # qr.wait() возвращает User
    client = FakeClient(qr=qr, user=user)
    worker = _worker(db, client)
    cmd_id = await _make_cmd(db)

    await worker._run_qr_login(await _get_cmd(db, cmd_id))

    cmd = await _get_cmd(db, cmd_id)
    assert cmd.status is LoginCommandStatus.authorized
    assert cmd.qr_link == "tg://login?token=1"
    acc = await _get_account(db)
    assert acc.status is TgAccountStatus.active
    assert acc.phone == "+79990000001"  # backfill из get_me()
    assert acc.display_name == "Иван"
    assert not client.connected  # disconnect ДО active


async def test_qr_already_authorized_session(db):
    client = FakeClient(authorized=True)
    worker = _worker(db, client)
    cmd_id = await _make_cmd(db)

    await worker._run_qr_login(await _get_cmd(db, cmd_id))
    cmd = await _get_cmd(db, cmd_id)
    assert cmd.status is LoginCommandStatus.authorized
    assert (await _get_account(db)).status is TgAccountStatus.active


async def test_qr_2fa_password(db):
    qr = FakeQr(outcomes=[SessionPasswordNeededError(request=None)])
    client = FakeClient(qr=qr)
    worker = _worker(db, client)
    cmd_id = await _make_cmd(db)

    async def feed_password():
        await asyncio.sleep(0.05)
        async with db.side() as s:
            await s.execute(
                LoginCommand.__table__.update()
                .where(LoginCommand.__table__.c.id == cmd_id)
                .values(password_transit="secret")
            )
            await s.commit()

    feeder = asyncio.create_task(feed_password())
    await worker._run_qr_login(await _get_cmd(db, cmd_id))
    feeder_err = None
    try:
        await feeder
    except BaseException as e:  # noqa: BLE001
        feeder_err = e
    if feeder_err:
        print("FEEDER FAILED:", repr(feeder_err))

    cmd = await _get_cmd(db, cmd_id)
    assert cmd.status is LoginCommandStatus.authorized, f"error={cmd.error!r}"
    assert cmd.password_transit is None  # стёрт при чтении


async def test_qr_recreate_on_timeout(db):

    class TimeoutQr(FakeQr):
        async def wait(self):
            raise TimeoutError()

    qr = TimeoutQr(outcomes=[])
    client = FakeClient(qr=qr)
    worker = _worker(db, client)
    cmd_id = await _make_cmd(db)

    await worker._run_qr_login(await _get_cmd(db, cmd_id))
    cmd = await _get_cmd(db, cmd_id)
    assert cmd.status is LoginCommandStatus.expired
    assert qr.recreate_calls == 2  # 3 итерации → 2 recreate


async def test_qr_refused_when_provider_active(db):
    client = FakeClient(qr=FakeQr(outcomes=[object()]))
    worker = _worker(db, client)
    worker._test_sm.providers[7] = object()
    cmd_id = await _make_cmd(db)

    await worker._run_qr_login(await _get_cmd(db, cmd_id))
    cmd = await _get_cmd(db, cmd_id)
    assert cmd.status is LoginCommandStatus.error
    assert "сначала отключите" in cmd.error


async def test_cancel_mid_scan(db):
    qr = FakeQr(outcomes=[])  # вечное ожидание
    client = FakeClient(qr=qr)
    worker = _worker(db, client)
    cmd_id = await _make_cmd(db)

    async def cancel_later():
        await asyncio.sleep(0.05)
        async with db.side() as s:
            await s.execute(
                LoginCommand.__table__.update()
                .where(LoginCommand.__table__.c.id == cmd_id)
                .values(cancel_requested=True)
            )
            await s.commit()

    canceller = asyncio.create_task(cancel_later())
    await worker._run_qr_login(await _get_cmd(db, cmd_id))
    await canceller
    cmd = await _get_cmd(db, cmd_id)
    assert cmd.status is LoginCommandStatus.cancelled


async def test_log_out_with_provider(db):
    class ProviderWithLogout:
        async def log_out(self):
            self.logged_out = True

    provider = ProviderWithLogout()
    client = FakeClient()
    worker = _worker(db, client)
    worker._test_sm.providers[7] = provider
    async with db() as s:
        acc = await s.get(TgAccount, 7)
        acc.status = TgAccountStatus.active
        await s.commit()
    cmd_id = await _make_cmd(db, kind=LoginCommandKind.log_out)

    await worker._run_log_out(await _get_cmd(db, cmd_id))

    cmd = await _get_cmd(db, cmd_id)
    assert cmd.status is LoginCommandStatus.done
    assert provider.logged_out
    assert worker._test_sync.forced == [(7, "admin unlink")]
    assert (await _get_account(db)).status is TgAccountStatus.offline


async def test_startup_selfheal_expires_active(db):
    worker = _worker(db, FakeClient())
    cmd_id = await _make_cmd(db)
    await worker._startup_selfheal()
    cmd = await _get_cmd(db, cmd_id)
    assert cmd.status is LoginCommandStatus.expired
