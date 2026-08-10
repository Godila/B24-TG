from unittest.mock import AsyncMock, MagicMock

import pytest

from app.bridge.session_manager import SessionManager


@pytest.mark.asyncio
async def test_register_and_get(monkeypatch):
    sm = SessionManager(api_id=1, api_hash="x", sessions_dir="/tmp")

    fake_provider = AsyncMock()
    fake_provider.connect = AsyncMock()

    monkeypatch.setattr(sm, "_build_provider", lambda account: fake_provider)

    account = MagicMock()
    account.id = 7
    account.phone = "+7000"

    await sm.register(account)
    assert sm.get(account.id) is fake_provider
    fake_provider.connect.assert_awaited_once()


@pytest.mark.asyncio
async def test_unregister_disconnects():
    sm = SessionManager(api_id=1, api_hash="x", sessions_dir="/tmp")
    fake_provider = AsyncMock()
    sm._providers[7] = fake_provider  # type: ignore

    await sm.unregister(7)
    fake_provider.disconnect.assert_awaited_once()
    assert sm.get(7) is None
