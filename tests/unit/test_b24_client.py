from unittest.mock import AsyncMock, patch

import pytest

from app.b24.client import Bitrix24Client, Bitrix24Error


@pytest.mark.asyncio
async def test_call_method_success():
    client = Bitrix24Client(client_endpoint="https://portal.bitrix24.ru/rest/")
    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_response.json = lambda: {"result": {"ID": "42"}, "time": {}}
    mock_response.raise_for_status = lambda: None

    with patch("app.b24.client.httpx.AsyncClient") as mock_httpx:
        mock_http = AsyncMock()
        mock_http.request = AsyncMock(return_value=mock_response)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_http)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=None)

        result = await client.call("crm.contact.list", auth_token="tok", params={"select[]": ["ID"]})

    assert result == {"ID": "42"}


@pytest.mark.asyncio
async def test_call_method_api_error():
    client = Bitrix24Client(client_endpoint="https://portal.bitrix24.ru/rest/")
    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_response.json = lambda: {"error": "NOT_FOUND", "error_description": "Not found."}
    mock_response.raise_for_status = lambda: None

    with patch("app.b24.client.httpx.AsyncClient") as mock_httpx:
        mock_http = AsyncMock()
        mock_http.request = AsyncMock(return_value=mock_response)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_http)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=None)

        with pytest.raises(Bitrix24Error) as exc:
            await client.call("crm.contact.get", auth_token="tok", params={"id": 999})
    assert exc.value.code == "NOT_FOUND"
