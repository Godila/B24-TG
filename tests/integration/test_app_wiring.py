import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool


async def _test_client(create_app) -> tuple[TestClient, object]:
    """TestClient с override get_session на пустой in-memory SQLite.

    План 009: /health ходит в БД (db-живость + счётчики tg_accounts) —
    дефолтный движок app.db без таблиц вернул бы 503 error/down.
    """
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        from app.models import Base

        await conn.run_sync(Base.metadata.create_all)
    SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    from app.db import get_session

    app = create_app()

    async def _override_session():
        async with SessionLocal() as s:
            yield s

    app.dependency_overrides[get_session] = _override_session
    return TestClient(app), engine


@pytest.fixture
async def client(monkeypatch):
    # Overrides над базой из conftest: dev-режим + конкретные CORS-origins.
    monkeypatch.setenv("DEV_MODE", "true")
    monkeypatch.setenv("CORS_ORIGINS", "https://b24-x.bitrix24.ru,http://localhost:5173")
    from app.config import get_settings

    get_settings.cache_clear()
    from app.web.app import create_app

    test_client, engine = await _test_client(create_app)
    yield test_client
    await engine.dispose()


def test_cors_headers_on_api(client):
    r = client.get("/health", headers={"Origin": "https://b24-x.bitrix24.ru"})
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == "https://b24-x.bitrix24.ru"


def test_cors_preflight_options(client):
    r = client.options(
        "/api/dialogs",
        headers={
            "Origin": "https://b24-x.bitrix24.ru",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert r.status_code == 200
    assert "access-control-allow-origin" in {k.lower() for k in r.headers}


async def test_cors_disabled_without_origins(monkeypatch):
    """Fail-closed: CORS_ORIGINS пуст → middleware не подключается, заголовков CORS нет."""
    monkeypatch.setenv("CORS_ORIGINS", "")
    from app.config import get_settings

    get_settings.cache_clear()
    from app.web.app import create_app

    client, engine = await _test_client(create_app)
    try:
        r = client.get("/health", headers={"Origin": "https://evil.example.com"})
        assert r.status_code == 200
        assert "access-control-allow-origin" not in {k.lower() for k in r.headers}
    finally:
        await engine.dispose()


def test_static_files_served(client):
    # Static dir may not exist yet (Task 7 creates assets). The mount should exist;
    # requesting a non-existent file returns 404 (not 500/crash).
    r = client.get("/static/nonexistent.js")
    assert r.status_code == 404


def test_dev_login_sets_cookie_and_redirects(client):
    r = client.get(
        "/dev/login",
        params={"b24_user_id": "15", "deal_id": "42"},
        follow_redirects=False,
    )
    assert r.status_code in (302, 307)
    cookie_header = r.headers.get("set-cookie", "")
    assert "btg_sess=" in cookie_header
    # Redirects to the static chat page.
    assert "/static/" in r.headers.get("location", "") or "/placement" in r.headers.get(
        "location", ""
    )


def test_dev_login_page_inbox_redirects_to_inbox(client):
    """page=inbox — dev-вход в общий мессенджер без реального B24."""
    r = client.get(
        "/dev/login",
        params={"b24_user_id": "15", "page": "inbox"},
        follow_redirects=False,
    )
    assert r.status_code in (302, 307)
    assert r.headers.get("location", "") == "/static/inbox.html"
    assert "btg_sess=" in r.headers.get("set-cookie", "")


def test_dev_login_disabled_in_prod(monkeypatch):
    monkeypatch.setenv("DEV_MODE", "false")
    from app.config import get_settings

    get_settings.cache_clear()
    from app.web.app import create_app

    client = TestClient(create_app())
    r = client.get("/dev/login", params={"b24_user_id": "15"}, follow_redirects=False)
    assert r.status_code == 404
