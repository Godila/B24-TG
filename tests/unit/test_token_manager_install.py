"""TokenManager.save_install_data: application_token сохраняется и не
затирается OAuth-refresh (refresh-ответ его не возвращает)."""


import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.b24.token_manager import TokenManager
from app.models import B24Token, Base

INSTALL_AUTH = {
    "access_token": "a1",
    "refresh_token": "r1",
    "member_id": "m1",
    "client_endpoint": "https://p/rest/",
    "domain": "p",
    "user_id": 1,
    "scope": "im",
    "expires_in": 3600,
    "application_token": "apptok-123",
}


@pytest.fixture
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


@pytest.mark.asyncio
async def test_save_install_data_persists_application_token(db, monkeypatch):
    monkeypatch.setattr("app.b24.token_manager.async_session", db)
    tm = TokenManager(client_id="cid", client_secret="cs")
    await tm.save_install_data(INSTALL_AUTH)

    async with db() as s:
        row = (await s.execute(select(B24Token))).scalar_one()
        assert row.application_token == "apptok-123"


@pytest.mark.asyncio
async def test_oauth_refresh_keeps_application_token(db, monkeypatch):
    """Refresh (oauth.bitrix24.tech) не возвращает application_token —
    _save_to_db обязан хранить прежний, а не перезаписывать None."""
    monkeypatch.setattr("app.b24.token_manager.async_session", db)
    tm = TokenManager(client_id="cid", client_secret="cs")
    await tm.save_install_data(INSTALL_AUTH)

    await tm._save_to_db(
        {
            "access_token": "a2",
            "refresh_token": "r2",
            "member_id": "m1",
            "expires_in": 3600,
        }
    )
    async with db() as s:
        row = (await s.execute(select(B24Token))).scalar_one()
        assert row.access_token == "a2"
        assert row.application_token == "apptok-123"


@pytest.mark.asyncio
async def test_reinstall_without_application_token_clears_it(db, monkeypatch):
    monkeypatch.setattr("app.b24.token_manager.async_session", db)
    tm = TokenManager(client_id="cid", client_secret="cs")
    await tm.save_install_data(INSTALL_AUTH)
    payload = {**INSTALL_AUTH, "access_token": "a3"}
    payload.pop("application_token")
    await tm.save_install_data(payload)

    async with db() as s:
        row = (await s.execute(select(B24Token))).scalar_one()
        assert row.application_token is None
