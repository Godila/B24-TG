from unittest.mock import AsyncMock, patch

import pytest

from app.b24.client import Bitrix24Client, Bitrix24Error


def _patch_httpx(mock_response):
    """Общий паттерн: подменить httpx.AsyncClient, вернуть мок request()."""
    patcher = patch("app.b24.client.httpx.AsyncClient")
    mock_httpx = patcher.start()
    mock_http = AsyncMock()
    mock_http.request = AsyncMock(return_value=mock_response)
    mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_http)
    mock_httpx.return_value.__aexit__ = AsyncMock(return_value=None)
    return patcher, mock_http.request


@pytest.mark.asyncio
async def test_call_method_success():
    client = Bitrix24Client(client_endpoint="https://portal.bitrix24.ru/rest/")
    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_response.json = lambda: {"result": {"ID": "42"}, "time": {}}

    patcher, request_mock = _patch_httpx(mock_response)
    try:
        result = await client.call("crm.contact.list", auth_token="tok", params={"select": ["ID"]})
    finally:
        patcher.stop()

    assert result == {"ID": "42"}
    body = request_mock.call_args.kwargs["json"]
    assert body == {"auth": "tok", "select": ["ID"]}


@pytest.mark.asyncio
async def test_call_method_api_error():
    client = Bitrix24Client(client_endpoint="https://portal.bitrix24.ru/rest/")
    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_response.json = lambda: {"error": "NOT_FOUND", "error_description": "Not found."}

    patcher, _ = _patch_httpx(mock_response)
    try:
        with pytest.raises(Bitrix24Error) as exc:
            await client.call("crm.contact.get", auth_token="tok", params={"id": 999})
    finally:
        patcher.stop()
    assert exc.value.code == "NOT_FOUND"


@pytest.mark.asyncio
async def test_call_sends_json_body_with_native_fields():
    """Тело запроса — application/json: вложенные структуры (fields)
    уходят нативными объектами. Form-кодирование ломало crm.item.add:
    str(dict) даёт python-repr, JSON-строка в form-поле тоже отвергается
    (error 100 — найдено спайком на проде, план 003)."""
    client = Bitrix24Client(client_endpoint="https://portal.bitrix24.ru/rest/")
    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_response.json = lambda: {"result": {"item": {"id": 1}}, "time": {}}

    patcher, request_mock = _patch_httpx(mock_response)
    try:
        await client.call(
            "crm.item.add",
            auth_token="tok",
            params={
                "entityTypeId": 3,
                "fields": {"NAME": "Тест", "PHONE": [{"VALUE": "+7999"}]},
            },
        )
    finally:
        patcher.stop()

    assert "data" not in request_mock.call_args.kwargs
    body = request_mock.call_args.kwargs["json"]
    assert body["fields"] == {"NAME": "Тест", "PHONE": [{"VALUE": "+7999"}]}
    assert body["entityTypeId"] == 3
    assert body["auth"] == "tok"
