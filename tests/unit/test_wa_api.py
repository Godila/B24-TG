"""Юнит-тесты OpenWaClient: контракты REST + нормализация ошибок."""

import json as jsonlib

import httpx
import pytest

from app.messaging.whatsapp.api import OpenWaClient, WaAuthError, WaError


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, content=None, headers=None):
        self.status_code = status_code
        self._json = json_data
        if content is not None:
            self.content = content
        elif json_data is not None:
            self.content = jsonlib.dumps(json_data).encode()
        else:
            self.content = b""
        base = "application/json" if json_data is not None else "application/octet-stream"
        self.headers = httpx.Headers({"content-type": base, **(headers or {})})

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


class FakeHttp:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    async def request(self, method, path, json=None, headers=None):
        self.requests.append((method, path, json, dict(headers or {})))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def make_client(*responses):
    fake = FakeHttp(responses)
    return OpenWaClient(
        base_url="http://openwa:2785", api_key="k1", http_factory=lambda: fake
    ), fake


async def test_get_session_sends_api_key_and_parses():
    client, fake = make_client(FakeResponse(json_data={"id": "s1", "status": "ready"}))
    info = await client.get_session("s1")
    assert info["status"] == "ready"
    method, path, _, headers = fake.requests[0]
    assert (method, path) == ("GET", "/api/sessions/s1")
    assert headers["X-API-Key"] == "k1"


async def test_409_is_retryable():
    client, _ = make_client(FakeResponse(status_code=409, json_data={"message": "conflict"}))
    with pytest.raises(WaError) as exc:
        await client.get_session("s1")
    assert exc.value.retryable is True
    assert "conflict" in str(exc.value)


async def test_429_maps_retry_after():
    client, _ = make_client(
        FakeResponse(status_code=429, json_data={}, headers={"Retry-After": "17"})
    )
    with pytest.raises(WaError) as exc:
        await client.send_text("s1", "628@c.us", "hi")
    assert exc.value.retry_after_sec == 17


async def test_429_without_header_defaults_30():
    client, _ = make_client(FakeResponse(status_code=429, json_data={}))
    with pytest.raises(WaError) as exc:
        await client.send_text("s1", "628@c.us", "hi")
    assert exc.value.retry_after_sec == 30


async def test_401_is_auth_terminal():
    client, _ = make_client(FakeResponse(status_code=401, json_data={}))
    with pytest.raises(WaAuthError):
        await client.get_session("s1")


async def test_transport_error_is_retryable():
    client, _ = make_client(httpx.ConnectError("boom"))
    with pytest.raises(WaError) as exc:
        await client.get_session("s1")
    assert exc.value.retryable is True


async def test_send_text_payload():
    client, fake = make_client(
        FakeResponse(status_code=201, json_data={"messageId": "m1", "timestamp": 1})
    )
    resp = await client.send_text("s1", "62812@c.us", "привет")
    assert resp["messageId"] == "m1"
    _, path, body, _ = fake.requests[0]
    assert path == "/api/sessions/s1/messages/send-text"
    assert body == {"chatId": "62812@c.us", "text": "привет"}


async def test_send_media_flat_dto():
    client, fake = make_client(
        FakeResponse(status_code=201, json_data={"messageId": "m2"})
    )
    await client.send_media(
        "s1",
        "62812@c.us",
        kind="image",
        b64="QUJD",
        mimetype="image/png",
        filename="p.png",
        caption="смотри",
    )
    _, path, body, _ = fake.requests[0]
    assert path == "/api/sessions/s1/messages/send-image"
    assert body == {
        "chatId": "62812@c.us",
        "base64": "QUJD",
        "mimetype": "image/png",
        "filename": "p.png",
        "caption": "смотри",
    }


async def test_create_session_with_proxy():
    client, fake = make_client(FakeResponse(status_code=201, json_data={"id": "s2"}))
    await client.create_session("line-3", proxy_url="socks5://xray-client:10808")
    _, _, body, _ = fake.requests[0]
    assert body == {
        "name": "line-3",
        "proxyUrl": "socks5://xray-client:10808",
        "proxyType": "socks5",
    }


async def test_download_media_returns_bytes_and_ctype():
    client, _ = make_client(
        FakeResponse(content=b"jpegbytes", headers={"content-type": "image/jpeg"})
    )
    data, ctype = await client.download_media("s1", "62812@c.us", "m9")
    assert data == b"jpegbytes"
    assert ctype == "image/jpeg"


async def test_non_json_body_on_json_route_raises():
    client, _ = make_client(FakeResponse(content=b"oops"))
    with pytest.raises(WaError):
        await client.get_session("s1")
