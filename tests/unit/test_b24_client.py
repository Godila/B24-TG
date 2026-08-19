from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.b24.client import Bitrix24Client, Bitrix24Error


def _patch_httpx(mock_response=None):
    """Общий паттерн: подменить httpx.AsyncClient.

    Клиент создаёт ``httpx.AsyncClient`` в ``__init__`` (shared-коннекты),
    поэтому патчить нужно ДО конструирования Bitrix24Client: тогда
    ``client._http`` — это наш ``mock_http``.
    """
    patcher = patch("app.b24.client.httpx.AsyncClient")
    mock_httpx = patcher.start()
    mock_http = MagicMock()
    mock_http.request = AsyncMock(return_value=mock_response)
    mock_http.aclose = AsyncMock(return_value=None)
    mock_httpx.return_value = mock_http
    return patcher, mock_http


def _make_response(payload: dict):
    resp = MagicMock()
    resp.status_code = 200
    resp.json = lambda: payload
    return resp


@pytest.mark.asyncio
async def test_call_method_success():
    patcher, mock_http = _patch_httpx(_make_response({"result": {"ID": "42"}, "time": {}}))
    try:
        client = Bitrix24Client(client_endpoint="https://portal.bitrix24.ru/rest/", min_interval=0)
        result = await client.call("crm.contact.list", auth_token="tok", params={"select": ["ID"]})
    finally:
        patcher.stop()

    assert result == {"ID": "42"}
    body = mock_http.request.call_args.kwargs["json"]
    assert body == {"auth": "tok", "select": ["ID"]}


@pytest.mark.asyncio
async def test_call_method_api_error():
    patcher, _ = _patch_httpx(
        _make_response({"error": "NOT_FOUND", "error_description": "Not found."})
    )
    try:
        client = Bitrix24Client(client_endpoint="https://portal.bitrix24.ru/rest/", min_interval=0)
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
    patcher, mock_http = _patch_httpx(_make_response({"result": {"item": {"id": 1}}, "time": {}}))
    try:
        client = Bitrix24Client(client_endpoint="https://portal.bitrix24.ru/rest/", min_interval=0)
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

    assert "data" not in mock_http.request.call_args.kwargs
    body = mock_http.request.call_args.kwargs["json"]
    assert body["fields"] == {"NAME": "Тест", "PHONE": [{"VALUE": "+7999"}]}
    assert body["entityTypeId"] == 3
    assert body["auth"] == "tok"


@pytest.mark.asyncio
async def test_min_interval_enforced_between_fast_calls():
    """Между двумя быстрыми вызовами выдерживается min_interval: второй
    call ждёт ~0.6с (sleep под моком — тест не тормозит)."""
    patcher, mock_http = _patch_httpx(_make_response({"result": 1, "time": {}}))
    sleep_patch = patch("app.b24.client.asyncio.sleep", new=AsyncMock())
    sleep_mock = sleep_patch.start()
    try:
        client = Bitrix24Client(
            client_endpoint="https://portal.bitrix24.ru/rest/", min_interval=0.6
        )
        await client.call("app.info", auth_token="tok")   # первый: ждать нечего
        await client.call("app.info", auth_token="tok")   # второй: пауза ~0.6с
    finally:
        sleep_patch.stop()
        patcher.stop()

    waits = [c.args[0] for c in sleep_mock.await_args_list if c.args]
    # Первый вызов sleep не делает (last_call=0.0 << monotonic), второй ждёт
    # остаток интервала: (0, 0.6].
    assert len(waits) == 1
    # +eps: monotonic между вызовами дрейфует на наносекунды — жёсткое
    # «<= 0.6» флакует (0.60000000009…).
    assert 0 < waits[0] <= 0.6 + 1e-6
    assert mock_http.request.await_count == 2


@pytest.mark.asyncio
async def test_query_limit_exceeded_retries_once_and_succeeds():
    """QUERY_LIMIT_EXCEEDED (в 200-теле) — один повтор после паузы 1.5с."""
    limited = _make_response({"error": "QUERY_LIMIT_EXCEEDED", "error_description": ""})
    ok = _make_response({"result": 7, "time": {}})
    patcher, mock_http = _patch_httpx()
    mock_http.request = AsyncMock(side_effect=[limited, ok])
    sleep_patch = patch("app.b24.client.asyncio.sleep", new=AsyncMock())
    sleep_mock = sleep_patch.start()
    try:
        client = Bitrix24Client(client_endpoint="https://portal.bitrix24.ru/rest/", min_interval=0)
        result = await client.call("crm.item.get", auth_token="tok", params={"id": 1})
    finally:
        sleep_patch.stop()
        patcher.stop()

    assert result == 7
    assert mock_http.request.await_count == 2
    # Пауза перед повтором — 1.5с (замокана, реального ожидания нет).
    assert 1.5 in [c.args[0] for c in sleep_mock.await_args_list if c.args]


@pytest.mark.asyncio
async def test_query_limit_exceeded_twice_raises():
    """Второй QUERY_LIMIT_EXCEEDED подряд — обычная Bitrix24Error (без
    бесконечных ретраев)."""
    limited = _make_response({"error": "QUERY_LIMIT_EXCEEDED", "error_description": "Too many"})
    patcher, mock_http = _patch_httpx()
    mock_http.request = AsyncMock(side_effect=[limited, limited])
    sleep_patch = patch("app.b24.client.asyncio.sleep", new=AsyncMock())
    sleep_patch.start()
    try:
        client = Bitrix24Client(client_endpoint="https://portal.bitrix24.ru/rest/", min_interval=0)
        with pytest.raises(Bitrix24Error) as exc:
            await client.call("crm.item.get", auth_token="tok", params={"id": 1})
    finally:
        sleep_patch.stop()
        patcher.stop()

    assert exc.value.code == "QUERY_LIMIT_EXCEEDED"
    assert mock_http.request.await_count == 2  # ровно один повтор


@pytest.mark.asyncio
async def test_aclose_closes_shared_http_client():
    patcher, mock_http = _patch_httpx()
    try:
        client = Bitrix24Client(client_endpoint="https://portal.bitrix24.ru/rest/")
        await client.aclose()
    finally:
        patcher.stop()
    mock_http.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_call_non_json_response_raises_b24_error():
    """HTML-ответ (мёртвый портал/заглушка домена) — Bitrix24Error
    invalid_response, а не голый JSONDecodeError с 500 (кейс переноса стенда)."""
    import json as _json

    def _raise():
        raise _json.JSONDecodeError("Expecting value", "<html>", 0)

    resp = MagicMock()
    resp.status_code = 502
    resp.json = _raise
    patcher, _ = _patch_httpx(resp)
    try:
        client = Bitrix24Client(client_endpoint="https://dead.bitrix24.ru/rest/", min_interval=0)
        with pytest.raises(Bitrix24Error) as ei:
            await client.call("user.get", auth_token="tok")
    finally:
        patcher.stop()

    assert ei.value.code == "invalid_response"
    assert "не JSON" in ei.value.description
