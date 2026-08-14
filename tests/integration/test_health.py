"""/health: базовый контракт на пустой БД (подробные DB-сценарии — test_health_db.py)."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool


@pytest.fixture
async def client():
    """Пустая in-memory SQLite (таблицы созданы) + override get_session.

    Без override ``app.db`` движок создаётся на DATABASE_URL из BASE_ENV,
    но без ``create_all`` запрос статусов аккаунтов падал бы «no such table».
    """
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        from app.models import Base

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
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    await engine.dispose()


def test_health_endpoint(client):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["db"] == "ok"
    assert body["accounts"] == {"total": 0, "active": 0, "offline": 0, "banned": 0}
