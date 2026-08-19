"""Unit-тесты HealthChecker: наблюдатель — алерты transition-only, БД не трогает.

Инцидент 08-18: чекер, писавший offline по факту дисконнекта, вышибал
аккаунт с живым токеном из active-сета навсегда. Теперь статус в БД —
неприкосновенен (его пишут только failure-hook и QR-флоу).
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


class FakeProvider:
    """Фейк под контракт ABC: sync is_connected(), состояние переключаемое."""

    def __init__(self, connected: bool = True):
        self._connected = connected

    def is_connected(self) -> bool:
        return self._connected


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
                messenger="tg",
                phone="+79990000001",
                session_path="/tmp/s7",
                manager_id=1,
                status=status,
            )
        )
        await s.commit()
    return 7


def _make_sm(provider: FakeProvider) -> SessionManager:
    sm = SessionManager(api_id=1, api_hash="x", sessions_dir="/tmp")
    sm._providers[7] = provider  # type: ignore[assignment]
    return sm


async def _get_status(SessionLocal, account_id: int) -> TgAccountStatus:
    async with SessionLocal() as s:
        account = (
            await s.execute(select(TgAccount).where(TgAccount.id == account_id))
        ).scalar_one()
        return account.status


@pytest.mark.asyncio
async def test_connected_account_no_alert(db):
    """Подключённый аккаунт: алерта нет, статус в БД не тронут."""
    await _seed_account(db, TgAccountStatus.active)
    notifier = AsyncMock()
    checker = HealthChecker(
        _make_sm(FakeProvider(connected=True)),
        session_factory=db,
        notifier=notifier,
        admin_user_id=1,
    )

    await checker._check_once()

    assert await _get_status(db, 7) is TgAccountStatus.active
    notifier.assert_not_awaited()


@pytest.mark.asyncio
async def test_disconnect_alerts_but_never_touches_db(db):
    """РЕГРЕССИЯ инцидента 08-18: дисконнект = алерт, но статус active
    в БД остаётся (аккаунт не вылетает из active-сета AccountSync)."""
    await _seed_account(db, TgAccountStatus.active)
    notifier = AsyncMock()
    checker = HealthChecker(
        _make_sm(FakeProvider(connected=False)),
        session_factory=db,
        notifier=notifier,
        admin_user_id=42,
    )

    await checker._check_once()

    assert await _get_status(db, 7) is TgAccountStatus.active  # ключевое
    notifier.assert_awaited_once()
    user_id, text = notifier.await_args.args
    assert user_id == 42
    assert "id=7" in text
    assert "+79990000001" in text
    assert "TG" in text


@pytest.mark.asyncio
async def test_alert_transition_only(db):
    """Повторные проверки дисконнекта — без повторных алертов."""
    await _seed_account(db, TgAccountStatus.active)
    notifier = AsyncMock()
    checker = HealthChecker(
        _make_sm(FakeProvider(connected=False)),
        session_factory=db,
        notifier=notifier,
        admin_user_id=1,
    )

    await checker._check_once()  # переход → алерт
    await checker._check_once()  # всё ещё дисконнект → молчит

    notifier.assert_awaited_once()


@pytest.mark.asyncio
async def test_restore_no_alert_then_realert_on_new_drop(db):
    """Реконнект — молча; новый обрыв после него — снова алерт."""
    await _seed_account(db, TgAccountStatus.active)
    provider = FakeProvider(connected=False)
    notifier = AsyncMock()
    checker = HealthChecker(
        _make_sm(provider), session_factory=db, notifier=notifier, admin_user_id=1
    )

    await checker._check_once()  # алерт №1
    provider._connected = True
    await checker._check_once()  # восстановление — молча
    provider._connected = False
    await checker._check_once()  # новый обрыв — алерт №2

    assert notifier.await_count == 2


@pytest.mark.asyncio
async def test_no_notifier_no_crash(db):
    """notifier=None: только логи, ничего не падает, БД не тронута."""
    await _seed_account(db, TgAccountStatus.active)
    checker = HealthChecker(
        _make_sm(FakeProvider(connected=False)),
        session_factory=db,
    )

    await checker._check_once()

    assert await _get_status(db, 7) is TgAccountStatus.active


@pytest.mark.asyncio
async def test_legacy_mode_without_session_factory():
    """Без session_factory: алерт без метаданных («?»), БД вообще не нужна."""
    notifier = AsyncMock()
    checker = HealthChecker(
        _make_sm(FakeProvider(connected=False)),
        notifier=notifier,
        admin_user_id=1,
    )
    await checker._check_once()
    notifier.assert_awaited_once()
    assert "id=7" in notifier.await_args.args[1]
