"""Интеграционные тесты /api/templates: список + фильтр по category.

Эндпоинту нужен auth + Manager. Используем ``app.dependency_overrides``,
чтобы подменить ``get_session`` in-memory SQLite-сессией и
``get_current_manager`` — засеянным менеджером (без проверки куки).
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool


@pytest.fixture
async def client():
    """Поднимает in-memory SQLite, сеет шаблоны, override'ит зависимости.

    Базовое окружение выставляет conftest (_hermetic_env).
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

    # Seed: manager (id=1, b24_user_id=15) + три шаблона (general x2, hold x1).
    from app.models import Manager, Template

    async with SessionLocal() as s:
        s.add(Manager(id=1, name="Иван", b24_user_id=15, is_active=True))
        s.add(
            Template(
                id=1,
                title="Приветствие",
                body="Здравствуйте! Чем помочь?",
                category="general",
            )
        )
        s.add(
            Template(
                id=2,
                title="Прощание",
                body="Спасибо за обращение!",
                category="general",
            )
        )
        s.add(
            Template(
                id=3,
                title="Ожидание",
                body="Уточняю информацию, минуточку...",
                category="hold",
            )
        )
        await s.commit()

    from app.db import get_session
    from app.web.app import create_app
    from app.web.deps import get_current_manager

    app = create_app()

    async def _override_session():
        async with SessionLocal() as s:
            yield s

    async def _override_manager():
        # Возвращаем засеянного менеджера без проверки куки (тестовый shortcut).
        async with SessionLocal() as s:
            res = await s.execute(select(Manager).where(Manager.id == 1))
            return res.scalar_one()

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_current_manager] = _override_manager

    client = TestClient(app)
    yield client
    app.dependency_overrides.clear()
    await engine.dispose()


def test_list_templates(client):
    r = client.get("/api/templates")
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 3
    titles = {t["title"] for t in data}
    assert "Приветствие" in titles
    for t in data:
        assert "id" in t
        assert "title" in t
        assert "body" in t


def test_list_templates_filter_by_category(client):
    r = client.get("/api/templates", params={"category": "hold"})
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 1
    assert data[0]["category"] == "hold"
    assert data[0]["title"] == "Ожидание"


def test_list_templates_empty_when_no_match(client):
    r = client.get("/api/templates", params={"category": "nonexistent"})
    assert r.status_code == 200
    assert r.json() == []
