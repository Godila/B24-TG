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
async def test_call_encodes_dict_params_as_json():
    """B24 принимает fields только JSON-строкой в form-теле: python-repr
    (одинарные кавычки) портал отвергает ошибкой 100 (spike, план 003)."""
    client = Bitrix24Client(client_endpoint="https://portal.bitrix24.ru/rest/")
    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_response.json = lambda: {"result": {"item": {"id": 1}}, "time": {}}

    patcher, request_mock = _patch_httpx(mock_response)
    try:
        await client.call(
            "crm.item.add",
            auth_token="tok",
            params={"entityTypeId": 3, "fields": {"NAME": "Тест", "PHONE": [{"VALUE": "+7999"}]}},
        )
    finally:
        patcher.stop()

    data = request_mock.call_args.kwargs["data"]
    assert data["fields"] == '{"NAME": "Тест", "PHONE": [{"VALUE": "+7999"}]}'
    assert data["entityTypeId"] == 3
    assert data["auth"] == "tok"


@pytest.mark.asyncio
async def test_call_keeps_list_params_for_form_encoding():
    """Списки остаются списками — httpx множит их в повторяющиеся поля
    формы (``values[]`` — рабочий формат findbycomm, подтверждён спайком)."""
    client = Bitrix24Client(client_endpoint="https://portal.bitrix24.ru/rest/")
    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_response.json = lambda: {"result": {"CONTACT": []}, "time": {}}

    patcher, request_mock = _patch_httpx(mock_response)
    try:
        await client.call(
            "crm.duplicate.findbycomm",
            auth_token="tok",
            params={"type": "PHONE", "values[]": ["+79990000000"]},
        )
    finally:
        patcher.stop()

    data = request_mock.call_args.kwargs["data"]
    assert data["values[]"] == ["+79990000000"]
    assert data["type"] == "PHONE"
