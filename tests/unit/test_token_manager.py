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

    with patch("app.b24.token_manager.httpx.get", return_value=mock_refresh_resp):
        tm._save_to_db = AsyncMock()
        token = await tm.get_token()

    assert token.access_token == "new_access"
    assert token.refresh_token == "new_refresh"
    tm._save_to_db.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_token_returns_none_if_not_installed():
    tm = TokenManager(client_id="c", client_secret="s")
    tm._load_from_db = AsyncMock(return_value=None)
    token = await tm.get_token()
    assert token is None
