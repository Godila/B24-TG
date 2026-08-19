from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.b24.token_manager import TokenManager
from app.models import B24Token


def _make_token(expired: bool = False) -> B24Token:
    delta = timedelta(minutes=-5) if expired else timedelta(hours=1)
    return B24Token(
        id=1,
        member_id="m1",
        access_token="old_access",
        refresh_token="old_refresh",
        client_endpoint="https://portal.bitrix24.ru/rest/",
        portal="https://portal.bitrix24.ru",
        user_id=1,
        scope="crm",
        expires_at=datetime.now(UTC) + delta,
    )


@pytest.mark.asyncio
async def test_get_valid_token_no_refresh():
    tm = TokenManager(client_id="c", client_secret="s")
    tm._load_from_db = AsyncMock(return_value=_make_token(expired=False))
    token = await tm.get_token()
    assert token.access_token == "old_access"
    tm._load_from_db.assert_awaited_once()


@pytest.mark.asyncio
async def test_refresh_when_expired():
    tm = TokenManager(client_id="c", client_secret="s")
    expired = _make_token(expired=True)
    tm._load_from_db = AsyncMock(return_value=expired)

    mock_refresh_resp = MagicMock()
    mock_refresh_resp.json = lambda: {
        "access_token": "new_access",
        "refresh_token": "new_refresh",
        "expires_in": 3600,
        "member_id": "m1",
        "client_endpoint": "https://portal.bitrix24.ru/rest/",
    }
    mock_refresh_resp.raise_for_status = lambda: None

    persisted = B24Token(
        id=1,
        member_id="m1",
        access_token="new_access",
        refresh_token="new_refresh",
        client_endpoint="https://portal.bitrix24.ru/rest/",
        portal="",
        user_id=0,
        scope="",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    with patch("app.b24.token_manager.httpx.get", return_value=mock_refresh_resp):
        tm._save_to_db = AsyncMock(return_value=persisted)
        token = await tm.get_token()

    assert token is persisted
    assert token.access_token == "new_access"
    assert token.refresh_token == "new_refresh"
    tm._save_to_db.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_token_returns_none_if_not_installed():
    tm = TokenManager(client_id="c", client_secret="s")
    tm._load_from_db = AsyncMock(return_value=None)
    token = await tm.get_token()
    assert token is None


@pytest.mark.asyncio
async def test_save_install_data_removes_stale_portal_row():
    """Перенос стенда: install с новым member_id сносит строку прежнего
    портала — _load_from_db читает единственную строку без фильтра."""
    from sqlalchemy import Delete

    tm = TokenManager(client_id="c", client_secret="s")
    upserted = _make_token()
    upserted.member_id = "m2"
    tm._upsert = AsyncMock(return_value=upserted)

    session = AsyncMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    with patch("app.b24.token_manager.async_session", return_value=ctx):
        result = await tm.save_install_data({
            "access_token": "a", "refresh_token": "r", "member_id": "m2",
            "client_endpoint": "https://new/rest/", "domain": "new.portal",
            "user_id": "1", "expires_in": "3600", "scope": "crm",
        })

    assert result is upserted
    stmt = session.execute.await_args_list[0].args[0]
    assert isinstance(stmt, Delete)
    tm._upsert.assert_awaited_once()
    session.commit.assert_awaited_once()
