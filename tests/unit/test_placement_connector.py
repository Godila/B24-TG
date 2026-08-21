"""Слайдер SETTING_CONNECTOR: рендер (supervisor/не-supervisor) и save."""

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import (
    Base,
    Manager,
    ManagerRole,
    TgAccount,
    TgAccountStatus,
)


@pytest.fixture
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        s.add_all(
            [
                Manager(
                    b24_user_id=7,
                    name="Админ",
                    role=ManagerRole.supervisor,
                    is_active=True,
                ),
                Manager(
                    b24_user_id=8,
                    name="Менеджер",
                    role=ManagerRole.manager,
                    is_active=True,
                ),
                TgAccount(
                    id=1,
                    messenger="tg",
                    phone="79001112233",
                    status=TgAccountStatus.active,
                    display_name="Основная линия",
                ),
                TgAccount(
                    id=2,
                    messenger="max",
                    phone="79004445566",
                    status=TgAccountStatus.active,
                ),
                # Занят другой линией — должен рендериться disabled.
                TgAccount(
                    id=3,
                    messenger="tg",
                    phone="79007778899",
                    status=TgAccountStatus.active,
                    ol_line_id="55",
                    ol_active=True,
                ),
            ]
        )
        await s.commit()
    yield factory
    await engine.dispose()


@pytest.fixture
def client(db, monkeypatch):
    monkeypatch.setenv("DEV_MODE", "true")
    from app.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setattr("app.db.async_session", db)
    from app.web.routes import placement

    monkeypatch.setattr(placement, "async_session", db)
    monkeypatch.setattr(placement, "_b24_connector_calls", AsyncMock())
    from app.web.app import create_app

    yield TestClient(create_app())
    get_settings.cache_clear()


def _open_slider(client, user_id: int = 7):
    return client.post(
        "/placement/connector",
        data={
            "PLACEMENT": "SETTING_CONNECTOR",
            "PLACEMENT_OPTIONS": '{"LINE":107,"ACTIVE_STATUS":true}',
            "AUTH": f'{{"user_id": {user_id}}}',
        },
    )


def test_slider_render_supervisor(client):
    resp = _open_slider(client)
    assert resp.status_code == 200
    body = resp.text
    assert "линия 107" in body
    assert "79001112233" in body and "Основная линия" in body
    assert "79004445566" in body
    # Аккаунт #3 занят линией 55: radio disabled + пометка.
    assert "disabled" in body and "привязан к линии 55" in body
    assert "Не использовать ЧатМост" in body


def test_slider_render_non_supervisor_forbidden(client):
    resp = _open_slider(client, user_id=8)
    assert resp.status_code == 403


def test_slider_missing_line_400(client):
    resp = client.post(
        "/placement/connector",
        data={"PLACEMENT": "SETTING_CONNECTOR", "AUTH": '{"user_id": 7}'},
    )
    assert resp.status_code == 400


def test_slider_save_binds_account(client, db):
    _open_slider(client)  # ставит сессионную куку
    resp = client.post("/placement/connector/save", data={"line": "107", "account": "1"})
    assert resp.status_code == 200
    assert "Готово" in resp.text

    import asyncio

    async def check():
        async with db() as s:
            acc = await s.get(TgAccount, 1)
            assert acc.ol_line_id == "107" and acc.ol_active is True

    asyncio.run(check())


def test_slider_save_conflict_rejected(client, db):
    _open_slider(client)
    # Аккаунт 3 уже на линии 55; линию 107 пытаемся отдать аккаунту 1,
    # а затем линию 55 — аккаунту 1: конфликт с привязкой аккаунта 3.
    resp = client.post("/placement/connector/save", data={"line": "55", "account": "1"})
    assert resp.status_code == 200
    assert "уже привязана к другому аккаунту" in resp.text


def test_slider_save_unbind(client, db):
    _open_slider(client)
    resp = client.post("/placement/connector/save", data={"line": "55", "account": "none"})
    assert resp.status_code == 200
    assert "Привязка снята" in resp.text

    import asyncio

    async def check():
        async with db() as s:
            acc = await s.get(TgAccount, 3)
            assert acc.ol_line_id is None and acc.ol_active is False

    asyncio.run(check())


def test_slider_rebind_deactivates_old_line(client, db):
    """Перепривязка аккаунта 3 (линия 55) на линию 107: старая линия B24
    деактивируется — её чат не должен слать события в никуда."""
    _open_slider(client)
    resp = client.post("/placement/connector/save", data={"line": "107", "account": "3"})
    assert resp.status_code == 200

    from app.web.routes import placement

    calls = placement._b24_connector_calls.call_args_list
    activations = [(c.args[0], c.kwargs.get("active")) for c in calls]
    assert ("107", True) in activations
    assert ("55", False) in activations
