"""Интеграционные тесты аутентификации по сессионной куке.

Проверяем три ключевых сценария зависимости ``get_current_manager``:
- 401 без куки;
- 200 + данные менеджера по валидной куке;
- 401 при куке, подписанной другим секретом.

ВАЖНО: импорты ``app.db`` / ``app.web.deps`` отложены внутрь фикстуры ``client``,
т.к. ``app.db`` при импорте вызывает ``get_settings()`` (читает ``.env``).
Фикстура сначала выставляет переменные окружения и сбрасывает кэш настроек, и
только после этого импортирует модули приложения. ``app.web.session`` безопасен
для импорта на верхнем уровне (не тянет ``app.db``/конфиг).

Изолируемся от module-level ``engine`` из ``app.db``: подменяем ``get_session``
через ``dependency_overrides`` на сессию из in-memory SQLite (``StaticPool`` —
общий коннект для всех запросов в отдельном потоке TestClient).
"""

from typing import Annotated

import pytest

from app.web.session import SESSION_COOKIE, create_session_payload, sign_session

SECRET = "integration-test-secret"


@pytest.fixture
def client(monkeypatch):
    # Тесты подписывают куку секретом SECRET — переопределяем базу из conftest.
    # Прочее окружение выставляет autouse-фикстура _hermetic_env (до этой
    # фикстуры), кэш настроек она же и чистит.
    monkeypatch.setenv("SESSION_SECRET", SECRET)
    from app.config import get_settings

    get_settings.cache_clear()

    # Импортируем приложение только после настройки окружения.
    from fastapi import Depends, FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )
    from sqlalchemy.pool import StaticPool

    from app.db import get_session
    from app.models import Base, Manager
    from app.web.deps import get_current_manager

    CurrentManager = Annotated[Manager, Depends(get_current_manager)]

    # In-memory SQLite с общим коннектом — чтобы данные, записанные при посеве,
    # были видны запросам TestClient в другом потоке.
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SessionLocal = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )

    # Создаём таблицы и сеем менеджера (b24_user_id=15).
    import asyncio

    async def _seed() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with SessionLocal() as s:
            s.add(Manager(name="Иван", b24_user_id=15, is_active=True))
            await s.commit()

    asyncio.run(_seed())

    app = FastAPI()

    @app.get("/whoami")
    async def whoami(manager: CurrentManager):
        return {"b24_user_id": manager.b24_user_id, "name": manager.name}

    async def _override_get_session() -> AsyncSession:
        async with SessionLocal() as s:
            yield s

    app.dependency_overrides[get_session] = _override_get_session

    with TestClient(app) as test_client:
        yield test_client

    asyncio.run(engine.dispose())


def test_whoami_without_cookie_returns_401(client):
    r = client.get("/whoami")
    assert r.status_code == 401


def test_whoami_with_valid_cookie_returns_manager(client):
    payload = create_session_payload(b24_user_id=15, deal_id=100)
    token = sign_session(payload, secret=SECRET)
    r = client.get("/whoami", cookies={SESSION_COOKIE: token})
    assert r.status_code == 200
    assert r.json() == {"b24_user_id": 15, "name": "Иван"}


def test_whoami_with_bad_secret_returns_401(client):
    payload = create_session_payload(b24_user_id=15, deal_id=100)
    token = sign_session(payload, secret="wrong-secret")
    r = client.get("/whoami", cookies={SESSION_COOKIE: token})
    assert r.status_code == 401


def test_whoami_unknown_manager_returns_401(client):
    """Валидная кука, но менеджера с таким b24_user_id нет в БД."""
    payload = create_session_payload(b24_user_id=999, deal_id=100)
    token = sign_session(payload, secret=SECRET)
    r = client.get("/whoami", cookies={SESSION_COOKIE: token})
    assert r.status_code == 401
