"""401 на placement-маршрутах — HTML-страница-инструкция, не сырой JSON.

Невнесённый сотрудник открывает «ЧатМост» из меню B24: POST ставит куку,
но GET-вкладки падают на ManagerDep. Раньше в iframe светился голый JSON
«Менеджер не найден» — теперь страница-направление (DESIGN.md, UX-12).
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool


@pytest.fixture
async def client(monkeypatch):
    monkeypatch.setenv("DEV_MODE", "true")
    from app.config import get_settings

    get_settings.cache_clear()

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
    from app.web.app import create_app

    app = create_app()

    async def _override_session():
        async with SessionLocal() as s:
            yield s

    app.dependency_overrides[get_session] = _override_session
    yield TestClient(app)
    app.dependency_overrides.clear()
    await engine.dispose()


def test_no_cookie_shows_session_expired_page(client):
    r = client.get("/placement/chats")
    assert r.status_code == 401
    assert r.headers["content-type"].startswith("text/html")
    assert "Сессия истекла" in r.text
    assert "левого меню" in r.text


def test_unknown_manager_shows_registration_hint_with_id(client):
    from app.config import get_settings
    from app.web.session import SESSION_COOKIE, create_session_cookie_params

    params = create_session_cookie_params(
        b24_user_id=999,
        deal_id=None,
        secret=get_settings().session_secret,
        secure=False,
    )
    client.cookies.set(SESSION_COOKIE, params["value"])
    r = client.get("/placement/chats")
    assert r.status_code == 401
    assert r.headers["content-type"].startswith("text/html")
    assert "Вы не добавлены в ЧатМост" in r.text
    assert "#999" in r.text
    assert "Менеджеры" in r.text


def test_api_401_stays_json(client):
    """Регресс: человекочитаемая страница — только для placement-iframe;
    API-клиенты по-прежнему получают JSON."""
    r = client.get("/api/dialogs")
    assert r.status_code == 401
    assert r.headers["content-type"].startswith("application/json")
    assert "detail" in r.json()
