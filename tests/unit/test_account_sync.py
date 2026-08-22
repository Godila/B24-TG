"""AccountSyncWorker: подхват новых active-аккаунтов, выгрузка мёртвых."""

import asyncio
from unittest.mock import MagicMock

import pytest

from app.bridge.account_sync import AccountSyncWorker, _account_credential
from app.models import Messenger, TgAccount, TgAccountStatus


class FakeSm:
    def __init__(self):
        self._providers: dict[int, MagicMock] = {}
        self.registered: list[TgAccount] = []
        self.unregistered: list[int] = []

    def registered_ids(self):
        return set(self._providers)

    def iter_providers(self):
        return list(self._providers.items())

    def get(self, account_id):
        return self._providers.get(account_id)

    async def register(self, account):
        self.registered.append(account)
        provider = MagicMock()
        provider.is_connected.return_value = True
        provider.is_dead.return_value = False
        # Провайдер «работает» на креде своего канала (MAX token / WA session).
        provider.credential_token.return_value = _account_credential(account)
        self._providers[account.id] = provider
        return provider

    async def unregister(self, account_id):
        self.unregistered.append(account_id)
        self._providers.pop(account_id, None)


def _account(aid: int, *, messenger: Messenger = Messenger.max) -> TgAccount:
    acc = TgAccount(
        id=aid,
        messenger=messenger,
        phone=f"+79990000{aid:02d}",
        status=TgAccountStatus.active,
        manager_id=1,
        token="tok",
        device_id="dev",
    )
    return acc


def _make_worker(sm, accounts, failures=None):
    class _Result:
        def scalars(self):
            class _Scalars:
                def all(self):
                    return accounts

            return _Scalars()

        def scalar_one_or_none(self):
            return None

    class Ctx:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def execute(self, stmt):
            return _Result()

    def session_factory():
        return Ctx()

    forwarded = []

    async def forward(provider, account, handler):
        forwarded.append(account.id)
        await asyncio.sleep(60)  # имитация бесконечной подписки

    worker = AccountSyncWorker(
        sm=sm,
        handler=MagicMock(),
        session_factory=session_factory,
        forward=forward,
        interval_sec=0.01,
        on_register_failure=failures,
    )
    return worker, forwarded



@pytest.mark.asyncio
async def test_new_active_account_registered_and_forwarded():
    sm = FakeSm()
    acc = _account(7)
    worker, forwarded = _make_worker(sm, [acc])

    await worker._sync_once()
    await asyncio.sleep(0.05)  # дать forward-таске стартовать

    assert sm.registered == [acc]
    assert forwarded == [7]
    # повторный sync не дублирует
    await worker._sync_once()
    await asyncio.sleep(0.05)
    assert len(sm.registered) == 1
    assert forwarded == [7]
    await worker.cancel_forwards()


@pytest.mark.asyncio
async def test_register_failure_calls_hook():
    sm = FakeSm()

    async def failing_register(account):
        raise RuntimeError("connect failed")

    sm.register = failing_register
    failures = []

    async def hook(account, exc):
        failures.append((account.id, str(exc)))

    worker, forwarded = _make_worker(sm, [_account(8)], failures=hook)
    await worker._sync_once()

    assert failures and failures[0][0] == 8
    assert forwarded == []


@pytest.mark.asyncio
async def test_dead_provider_unregistered():
    sm = FakeSm()
    acc = _account(9)
    await sm.register(acc)
    sm._providers[9].is_dead.return_value = True
    worker, _fwd = _make_worker(sm, [])  # аккаунт исчез из active

    await worker._sync_once()

    assert sm.unregistered == [9]


@pytest.mark.asyncio
async def test_offline_alive_provider_kept():
    """offline-статус при живом провайдере = идёт реконнект — не трогаем."""
    sm = FakeSm()
    acc = _account(10)
    await sm.register(acc)
    worker, _fwd = _make_worker(sm, [])

    await worker._sync_once()

    assert sm.unregistered == []
    assert sm.get(10) is not None


@pytest.mark.asyncio
async def test_credentials_rotation_reregisters():
    """Менеджер перепривязался новым QR: токен в БД изменился — провайдер
    на старом токене снимается (новый подхватится следующим тиком)."""
    sm = FakeSm()
    acc = _account(11)
    await sm.register(acc)
    # Токен в БД обновился (новый QR), провайдер всё ещё на старом.
    acc.token = "tok-NEW"
    worker, _fwd = _make_worker(sm, [acc])

    await worker._sync_once()
    await asyncio.sleep(0.05)

    assert sm.unregistered == [11]


@pytest.mark.asyncio
async def test_disconnected_provider_revived_after_grace():
    """TG ходит с auto_reconnect=False: отвалившийся провайдер сам не
    лечится. Грейс 2 тика — не сбрасываем на мелькании, после — снимаем
    (следующий тик перерегистрирует)."""
    sm = FakeSm()
    acc = _account(21, messenger=Messenger.tg)
    await sm.register(acc)
    provider = sm.get(21)
    provider.is_connected.return_value = False
    provider.is_dead.return_value = False
    worker, _fwd = _make_worker(sm, [acc])

    await worker._sync_once()  # тик 1: грейс, не трогаем
    assert sm.unregistered == []
    assert sm.get(21) is not None

    await worker._sync_once()  # тик 2: порог — ревайв
    assert sm.unregistered == [21]
    assert sm.get(21) is None


@pytest.mark.asyncio
async def test_connected_provider_not_revived():
    sm = FakeSm()
    acc = _account(22)
    await sm.register(acc)
    worker, _fwd = _make_worker(sm, [acc])

    for _ in range(4):
        await worker._sync_once()

    assert sm.unregistered == []
    assert sm.get(22) is not None


@pytest.mark.asyncio
async def test_register_failure_hook_rate_limits_transient_alerts():
    """Транзиентные сбои ретраятся каждые ~20с: без rate-lock долговременный

    сетевой сбой заспамил бы админ-чат. Не-терминальные алерты — не чаще
    раза в transient_alert_repeat_sec; терминальные (MaxAuthError) — всегда."""
    from app.bridge.account_sync import make_register_failure_hook
    from app.messaging.max.protocol import MaxAuthError

    calls: list[int] = []

    async def notifier(user_id: int, text: str) -> None:
        calls.append(user_id)

    class _Ctx:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def execute(self, stmt):
            return None

        async def commit(self):
            return None

    def session_factory():
        return _Ctx()

    hook = make_register_failure_hook(
        session_factory, notifier, 42, transient_alert_repeat_sec=0.2
    )
    acc = _account(1)
    acc.messenger = Messenger.tg

    await hook(acc, TimeoutError("tunnel down"))
    await hook(acc, TimeoutError("tunnel down"))
    assert calls == [42]  # второй транзиентный подавлен

    await asyncio.sleep(0.25)
    await hook(acc, TimeoutError("tunnel down"))
    assert calls == [42, 42]  # окно истекло — снова доставлен

    await hook(acc, MaxAuthError("token revoked"))
    await hook(acc, MaxAuthError("token revoked"))
    assert calls == [42, 42, 42, 42]  # терминальные идут всегда


@pytest.mark.asyncio
async def test_register_failure_hook_tg_revoked_session_terminal():
    """Мёртвая TG-сессия — терминально: алерт всегда, с честным текстом

    (не «сетевой сбой»); MAX-ветка с MaxAuthError покрыта rate-limit-тестом."""
    from app.bridge.account_sync import make_register_failure_hook
    from app.messaging.provider import SessionRevokedError

    calls: list[str] = []

    async def notifier(user_id: int, text: str) -> None:
        calls.append(text)

    executed: list[object] = []

    class _Ctx:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def execute(self, stmt):
            executed.append(stmt)

        async def commit(self):
            return None

    def session_factory():
        return _Ctx()

    hook = make_register_failure_hook(session_factory, notifier, 42)
    acc = _account(31, messenger=Messenger.tg)

    await hook(acc, SessionRevokedError("TG session not authorized"))
    await hook(acc, SessionRevokedError("TG session not authorized"))
    assert len(calls) == 2  # терминальные — без rate-limit
    assert "сессия отозвана" in calls[0]
    assert "QR" in calls[0]
    # Оффлайн-запись в БД выполнена (аккаунт выпадает из ретраев).
    assert len(executed) == 2


# --- WhatsApp: restriction-колонки + сверка кредов по wa_session_id ---

@pytest.fixture
async def wa_db():
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from sqlalchemy.pool import StaticPool

    from app.models import Base, Manager

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
                id=30,
                messenger=Messenger.wa,
                phone="+70000000030",
                status=TgAccountStatus.active,
                manager_id=1,
                wa_session_id="s-30",
            )
        )
        await s.commit()
    yield factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_wa_restriction_persisted(wa_db):
    from sqlalchemy import select

    sm = FakeSm()
    acc = TgAccount(
        id=30,
        messenger=Messenger.wa,
        phone="+70000000030",
        status=TgAccountStatus.active,
        manager_id=1,
        wa_session_id="s-30",
    )
    await sm.register(acc)
    provider = sm._providers[30]
    provider.credential_token.return_value = "s-30"
    provider.restriction.return_value = {
        "kind": "reachout_timelock",
        "code": "BIZ_QUALITY",
        "expiresAt": "2026-09-01T00:00:00Z",
        "active": True,
    }
    worker = AccountSyncWorker(
        sm=sm, handler=MagicMock(), session_factory=wa_db, interval_sec=0.01
    )
    await worker._sync_once()
    assert sm.unregistered == []

    async with wa_db() as s:
        row = (await s.execute(select(TgAccount).where(TgAccount.id == 30))).scalar_one()
        assert row.restriction_kind == "reachout_timelock"
        assert row.restriction_until is not None
    await worker.cancel_forwards()


@pytest.mark.asyncio
async def test_wa_rebind_detected_by_session_id(wa_db):
    sm = FakeSm()
    acc = TgAccount(
        id=30,
        messenger=Messenger.wa,
        phone="+70000000030",
        status=TgAccountStatus.active,
        manager_id=1,
        wa_session_id="s-30",
    )
    await sm.register(acc)
    # Провайдер работает на старой сессии — строка перепривязана новым QR.
    sm._providers[30].credential_token.return_value = "s-NEW"
    worker = AccountSyncWorker(
        sm=sm, handler=MagicMock(), session_factory=wa_db, interval_sec=0.01
    )
    await worker._sync_once()
    assert sm.unregistered == [30]
    await worker.cancel_forwards()
