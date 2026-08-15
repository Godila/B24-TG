"""MaxUserProvider: INIT+LOGIN, send, push→очередь, завершение стрима."""

import asyncio
from typing import Any

import pytest

from app.messaging.max.protocol import OP_INIT, OP_LOGIN, OP_MSG_SEND, MaxAuthError
from app.messaging.max.provider import MaxUserProvider
from app.messaging.types import SendResult
from app.models import Messenger


class FakeMaxClient:
    """Скриптованный клиент: request отвечает по opcode; push — вручную."""

    def __init__(self, *, login_error: Exception | None = None):
        self.requests: list[tuple[int, dict]] = []
        self._login_error = login_error
        self._is_open = False
        self._on_push = None
        self.last_send = 0.0

    async def connect(self) -> None:
        self._is_open = True

    async def close(self) -> None:
        self._is_open = False

    def is_connected(self) -> bool:
        return self._is_open

    @property
    def closed(self) -> bool:
        return not self._is_open

    def on_push(self, cb) -> None:
        self._on_push = cb

    async def request(self, opcode: int, payload: dict | None = None, *, timeout: float | None = None) -> dict:
        self.requests.append((opcode, payload or {}))
        if opcode == OP_INIT:
            return {"cmd": 1, "seq": 0, "opcode": opcode, "payload": {}}
        if opcode == OP_LOGIN:
            if self._login_error is not None:
                raise self._login_error
            return {"cmd": 1, "seq": 0, "opcode": opcode, "payload": {"profile": {"id": 1}}}
        if opcode == OP_MSG_SEND:
            return {
                "cmd": 1, "seq": 0, "opcode": opcode,
                "payload": {"message": {"id": 117099065741753584, "time": 1786789943569}},
            }
        return {"cmd": 1, "seq": 0, "opcode": opcode, "payload": {}}


def _make_provider(client: FakeMaxClient | None = None) -> MaxUserProvider:
    return MaxUserProvider(
        token="An_test_token",
        device_id="dev-uuid-1",
        own_user_id=401041669,
        ws_url="wss://test",
        headers={"Origin": "https://web.max.ru"},
        user_agent={"appVersion": "26.8.4"},
        heartbeat_idle_sec=9999,  # heartbeat не мешает тестам
        client_factory=(lambda: client) if client is not None else None,
    )


@pytest.mark.asyncio
async def test_connect_sends_init_and_login():
    fake = FakeMaxClient()
    provider = _make_provider(fake)
    await provider.connect()
    try:
        opcodes = [op for op, _ in fake.requests]
        assert opcodes[:2] == [OP_INIT, OP_LOGIN]
        init_payload = fake.requests[0][1]
        assert init_payload["deviceId"] == "dev-uuid-1"
        assert init_payload["userAgent"]["appVersion"] == "26.8.4"
        assert fake.requests[1][1]["token"] == "An_test_token"
        assert provider.is_connected()
    finally:
        await provider.disconnect()


@pytest.mark.asyncio
async def test_auth_error_on_connect_marks_dead():
    fake = FakeMaxClient(login_error=MaxAuthError("token revoked"))
    provider = _make_provider(fake)
    with pytest.raises(MaxAuthError):
        await provider.connect()
    assert provider.is_dead()
    assert not provider.is_connected()


@pytest.mark.asyncio
async def test_send_message_ok_id_as_str():
    fake = FakeMaxClient()
    provider = _make_provider(fake)
    await provider.connect()
    try:
        result = await provider.send_message("422733600", "текст", is_initiation=False)
        assert isinstance(result, SendResult)
        assert result.success
        # id приходит ЧИСЛОМ — храним строкой.
        assert result.external_message_id == "117099065741753584"
        op, payload = fake.requests[-1]
        assert op == OP_MSG_SEND
        assert payload["chatId"] == 422733600
        assert payload["message"]["text"] == "текст"
        assert payload["notify"] is True
        # cid уникален при очереди
        first_cid = payload["message"]["cid"]
        await provider.send_message("422733600", "ещё", is_initiation=False)
        second_cid = fake.requests[-1][1]["message"]["cid"]
        assert first_cid != second_cid
    finally:
        await provider.disconnect()


@pytest.mark.asyncio
async def test_send_bad_chat_id():
    provider = _make_provider(FakeMaxClient())
    result = await provider.send_message("не-число", "x", is_initiation=False)
    assert not result.success
    assert "bad_chat_id" in (result.error or "")


@pytest.mark.asyncio
async def test_push_becomes_incoming_message():
    fake = FakeMaxClient()
    provider = _make_provider(fake)
    await provider.connect()
    try:
        frame = {
            "ver": 11, "cmd": 0, "seq": 1, "opcode": 128,
            "payload": {
                "chatId": 422733600,
                "chat": {"type": "DIALOG", "lastMessage": {
                    "sender": 248843813, "id": "m1", "time": 1786792936720,
                    "text": "привет", "type": "USER", "attaches": [],
                }},
            },
        }
        await fake._on_push(frame)
        msg = await asyncio.wait_for(provider._incoming_queue.get(), timeout=1)
        assert msg.messenger is Messenger.max
        assert msg.external_chat_id == "422733600"
        assert msg.sender_external_id == "248843813"
        assert msg.external_message_id == "m1"
        assert msg.text == "привет"
    finally:
        await provider.disconnect()


@pytest.mark.asyncio
async def test_self_push_filtered():
    fake = FakeMaxClient()
    provider = _make_provider(fake)
    await provider.connect()
    try:
        frame = {
            "opcode": 128,
            "payload": {"chatId": 1, "chat": {"type": "DIALOG", "lastMessage": {
                "sender": 401041669, "id": "m2", "text": "мой эхо",
                "type": "USER", "attaches": [],
            }}},
        }
        await fake._on_push(frame)
        assert provider._incoming_queue.empty()
    finally:
        await provider.disconnect()


@pytest.mark.asyncio
async def test_disconnect_ends_incoming_stream():
    fake = FakeMaxClient()
    provider = _make_provider(fake)
    await provider.connect()

    async def consume() -> list[Any]:
        out = []
        async for msg in provider.incoming_stream():
            out.append(msg)
        return out

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.01)
    await provider.disconnect()
    out = await asyncio.wait_for(task, timeout=1)
    assert out == []  # стрим ЗАКОНЧИЛСЯ, а не завис
