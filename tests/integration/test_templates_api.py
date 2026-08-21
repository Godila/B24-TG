"""Интеграционные тесты /api/templates: чтение + CRUD (supervisor-only).

Эндпоинту нужен auth + Manager. Используем ``app.dependency_overrides``,
чтобы подменить ``get_session`` in-memory SQLite-сессией и
``get_current_manager`` — засеянным менеджером (без проверки куки).
Фабрика делает клиента для обычного менеджера (id=1) и supervisor (id=2).
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool


@pytest.fixture
async def factory():
    """Поднимает in-memory SQLite, сеет менеджеров + шаблоны; отдаёт
    ``make(manager_id) -> TestClient`` с подменённым текущим менеджером."""
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

    # Seed: менеджер (id=1) + supervisor (id=2) + три шаблона (general x2, hold x1).
    from app.models import Manager, ManagerRole, Template

    async with SessionLocal() as s:
        s.add(Manager(id=1, name="Иван", b24_user_id=15, is_active=True))
        s.add(
            Manager(
                id=2,
                name="Ольга",
                b24_user_id=16,
                is_active=True,
                role=ManagerRole.supervisor,
            )
        )
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

    app.dependency_overrides[get_session] = _override_session

    def make(manager_id: int) -> TestClient:
        async def _override_manager():
            async with SessionLocal() as s:
                return await s.get(Manager, manager_id)

        app.dependency_overrides[get_current_manager] = _override_manager
        return TestClient(app)

    yield make
    app.dependency_overrides.clear()
    await engine.dispose()


@pytest.fixture
async def client(factory):
    return factory(1)


@pytest.fixture
async def su_client(factory):
    return factory(2)


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


# ---- CRUD (supervisor-only) ----


def test_create_template(su_client):
    r = su_client.post(
        "/api/templates",
        json={"title": "Цены", "body": "Прайс во вложении", "category": "sales"},
    )
    assert r.status_code == 201
    data = r.json()
    assert data["title"] == "Цены"
    assert data["category"] == "sales"
    assert data["id"] > 0


def test_create_template_strips_empty_category(su_client):
    r = su_client.post(
        "/api/templates", json={"title": "Без тега", "body": "Текст", "category": ""}
    )
    assert r.status_code == 201
    assert r.json()["category"] is None


def test_create_template_forbidden_for_manager(client):
    r = client.post(
        "/api/templates", json={"title": "X", "body": "Y"}
    )
    assert r.status_code == 403


def test_create_template_cross_origin_blocked(su_client):
    r = su_client.post(
        "/api/templates",
        json={"title": "X", "body": "Y"},
        headers={"Origin": "https://evil.example.com"},
    )
    assert r.status_code == 403


def test_create_template_validation(su_client):
    assert (
        su_client.post("/api/templates", json={"title": "", "body": "ok"}).status_code
        == 422
    )
    assert (
        su_client.post(
            "/api/templates", json={"title": "ok", "body": "x" * 4097}
        ).status_code
        == 422
    )
    assert (
        su_client.post(
            "/api/templates", json={"title": "ok", "body": "ok", "category": "c" * 65}
        ).status_code
        == 422
    )


def test_update_template_full_replace(su_client):
    r = su_client.put(
        "/api/templates/1",
        json={"title": "Привет!", "body": "Добрый день"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["title"] == "Привет!"
    assert data["body"] == "Добрый день"
    assert data["category"] is None  # полная замена: тег снят


def test_update_template_forbidden_for_manager(client):
    r = client.put(
        "/api/templates/1", json={"title": "X", "body": "Y"}
    )
    assert r.status_code == 403


def test_update_template_404(su_client):
    r = su_client.put(
        "/api/templates/999", json={"title": "X", "body": "Y"}
    )
    assert r.status_code == 404


def test_delete_template(su_client):
    r = su_client.delete("/api/templates/2")
    assert r.status_code == 200
    assert r.json() == {"status": "removed"}
    r2 = su_client.get("/api/templates")
    titles = {t["title"] for t in r2.json()}
    assert "Прощание" not in titles


def test_delete_template_forbidden_for_manager(client):
    assert client.delete("/api/templates/2").status_code == 403


def test_delete_template_404(su_client):
    assert su_client.delete("/api/templates/999").status_code == 404
