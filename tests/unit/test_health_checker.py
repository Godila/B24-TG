"""Unit-тесты HealthChecker (план 009): персист статусов + transition-only алерты.

Чекер ходит в реальную in-memory SQLite (StaticPool) через
``session_factory`` и зовёт ``notifier``-колбэк только на переходе
active→offline. Провайдеры — фейки с ``_client.is_connected()``.
"""

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.bridge.health_checker import HealthChecker
from app.bridge.session_manager import SessionManager
from app.models import Base, Manager, TgAccount, TgAccountStatus


class FakeClient:
    def __init__(self, connected: bool):
        self._connected = connected

    def is_connected(self) -> bool:
        return self._connected


class FakeProvider:
    def __init__(self, connected: bool):
        self._client = FakeClient(connected)


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


async def _seed_account(SessionLocal, status: TgAccountStatus) -> int:
    """Сеет менеджера + аккаунт id=7 с заданным статусом, возвращает id."""
    async with SessionLocal() as s:
        s.add(Manager(id=1, name="Иван", b24_user_id=15, is_active=True))
        s.add(
            TgAccount(
                id=7,
                phone="+79990000001",
                session_path="/tmp/s7",
                manager_id=1,
                status=status,
            )
        )
        await s.commit()
    return 7


def _make_sm(connected: bool) -> SessionManager:
    sm = SessionManager(api_id=1, api_hash="x", sessions_dir="/tmp")
    sm._providers[7] = FakeProvider(connected)  # type: ignore[assignment]
    return sm


async def _get_status(SessionLocal, account_id: int) -> TgAccountStatus:
    async with SessionLocal() as s:
        account = (
            await s.execute(select(TgAccount).where(TgAccount.id == account_id))
        ).scalar_one()
        return account.status


@pytest.mark.asyncio
async def test_connected_account_stays_active_and_no_alert(db):
    """Подключённый аккаунт: статус active (становится/остаётся), алерта нет."""
    account_id = await _seed_account(db, TgAccountStatus.active)
    notifier = AsyncMock()
    checker = HealthChecker(
        _make_sm(connected=True),
        session_factory=db,
        notifier=notifier,
        admin_user_id=1,
    )

    await checker._check_once()

    assert await _get_status(db, account_id) is TgAccountStatus.active
    notifier.assert_not_awaited()


@pytest.mark.asyncio
async def test_disconnect_persists_offline_and_alerts(db):
    """Отключение при active в БД: status=offline + алерт с account_id."""
    account_id = await _seed_account(db, TgAccountStatus.active)
    notifier = AsyncMock()
    checker = HealthChecker(
        _make_sm(connected=False),
        session_factory=db,
        notifier=notifier,
        admin_user_id=42,
    )

    await checker._check_once()

    assert await _get_status(db, account_id) is TgAccountStatus.offline
    notifier.assert_awaited_once()
    user_id, text = notifier.await_args.args
    assert user_id == 42
    assert "id=7" in text
    assert "+79990000001" in text


@pytest.mark.asyncio
async def test_no_notifier_no_crash(db):
    """notifier=None (старый режим): статус пишется, ничего не падает."""
    account_id = await _seed_account(db, TgAccountStatus.active)
    checker = HealthChecker(
        _make_sm(connected=False),
        session_factory=db,
    )

    await checker._check_once()  # не должен бросить

    assert await _get_status(db, account_id) is TgAccountStatus.offline


@pytest.mark.asyncio
async def test_alert_not_repeated_when_already_offline(db):
    """Transition-only: повторная проверка офлайн-аккаунта — БЕЗ алерта."""
    account_id = await _seed_account(db, TgAccountStatus.active)
    notifier = AsyncMock()
    checker = HealthChecker(
        _make_sm(connected=False),
        session_factory=db,
        notifier=notifier,
        admin_user_id=1,
    )

    await checker._check_once()  # active → offline: алерт
    await checker._check_once()  # уже offline: без алерта

    assert await _get_status(db, account_id) is TgAccountStatus.offline
    notifier.assert_awaited_once()


@pytest.mark.asyncio
async def test_recovery_updates_status_without_alert(db):
    """Обратный переход offline→active: просто UPDATE, алерта нет."""
    account_id = await _seed_account(db, TgAccountStatus.offline)
    notifier = AsyncMock()
    checker = HealthChecker(
        _make_sm(connected=True),
        session_factory=db,
        notifier=notifier,
        admin_user_id=1,
    )

    await checker._check_once()

    assert await _get_status(db, account_id) is TgAccountStatus.active
    notifier.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_mode_without_session_factory_only_logs():
    """Без session_factory (Фаза 1): только логирование, БД не трогается."""
    checker = HealthChecker(_make_sm(connected=False))
    await checker._check_once()  # не должен бросить
