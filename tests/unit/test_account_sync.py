"""AccountSyncWorker: подхват новых active-аккаунтов, выгрузка мёртвых."""

import asyncio
from unittest.mock import MagicMock

import pytest

from app.bridge.account_sync import AccountSyncWorker
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
        # Провайдер «работает» на токене аккаунта, с которым зарегистрирован.
        provider.credential_token.return_value = account.token
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
