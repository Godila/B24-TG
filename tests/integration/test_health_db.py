"""Интеграционные DB-тесты /health (план 009): реальные счётчики статусов.

Web-процесс не знает is_connected — он читает ``tg_accounts.status``
(пишут failure-hook терминальных auth-отказов и QR-флоу). Здесь сием
статусы напрямую и сверяем ответ эндпоинта: ok / degraded (503) /
error (БД недоступна).
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.models import Base, Manager, TgAccount, TgAccountStatus


@pytest.fixture
async def health_env():
    """In-memory SQLite со схемой + override get_session. ``(client, SessionLocal)``."""
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

    from app.db import get_session
    from app.web.app import create_app

    app = create_app()

    async def _override_session():
        async with SessionLocal() as s:
            yield s

    app.dependency_overrides[get_session] = _override_session
    client = TestClient(app)
    yield client, SessionLocal
    app.dependency_overrides.clear()
    await engine.dispose()


async def _seed_account(SessionLocal, account_id: int, status: TgAccountStatus) -> None:
    """Менеджер + аккаунт с заданным статусом (manager_id уникален per-account)."""
    async with SessionLocal() as s:
        s.add(
            Manager(
                id=account_id,
                name=f"M{account_id}",
                b24_user_id=100 + account_id,
                is_active=True,
            )
        )
        s.add(
            TgAccount(
                id=account_id,
                phone=f"+7999{account_id:08d}",
                session_path=f"/tmp/s{account_id}",
                manager_id=account_id,
                status=status,
            )
        )
        await s.commit()


async def test_health_empty_db_is_ok(health_env):
    """(a) Аккаунтов нет → ok 200 (total=0 — не degraded)."""
    client, _ = health_env
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {
        "status": "ok",
        "db": "ok",
        "accounts": {"total": 0, "active": 0, "offline": 0, "banned": 0},
        # media — информационное поле (чат живёт и без медиа-тома).
        "media": {"ok": True},
    }


async def test_health_one_active_is_ok(health_env):
    """(b) 1 active → ok 200."""
    client, SessionLocal = health_env
    await _seed_account(SessionLocal, 7, TgAccountStatus.active)

    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["db"] == "ok"
    assert body["accounts"] == {"total": 1, "active": 1, "offline": 0, "banned": 0}


async def test_health_no_active_account_is_degraded_503(health_env):
    """(c) 1 offline, 0 active → 503 degraded (bridge не отвечает ни одной сессией)."""
    client, SessionLocal = health_env
    await _seed_account(SessionLocal, 7, TgAccountStatus.offline)

    r = client.get("/health")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "degraded"
    assert body["db"] == "ok"
    assert body["accounts"] == {"total": 1, "active": 0, "offline": 1, "banned": 0}


async def test_health_banned_account_is_degraded(health_env):
    """(d) banned есть → degraded, даже при активном втором аккаунте."""
    client, SessionLocal = health_env
    await _seed_account(SessionLocal, 7, TgAccountStatus.active)
    await _seed_account(SessionLocal, 8, TgAccountStatus.banned)

    r = client.get("/health")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "degraded"
    assert body["accounts"] == {"total": 2, "active": 1, "offline": 0, "banned": 1}


async def test_health_db_down_returns_error_503():
    """БД недоступна → 503 + status=error/db=down (внешний монитор видит сбой)."""
    # Файл в несуществующем каталоге: SQLite не создаёт каталоги → ошибка
    # подключения на первом же execute.
    engine = create_async_engine("sqlite+aiosqlite:///./no_such_dir_zz/health.db")
    SessionLocal = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )

    from app.db import get_session
    from app.web.app import create_app

    app = create_app()

    async def _override_session():
        async with SessionLocal() as s:
            yield s

    app.dependency_overrides[get_session] = _override_session
    client = TestClient(app)
    try:
        r = client.get("/health")
    finally:
        app.dependency_overrides.clear()
        await engine.dispose()

    assert r.status_code == 503
    assert r.json() == {"status": "error", "db": "down"}
