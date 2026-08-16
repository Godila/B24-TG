"""MaxUserProvider: INIT+LOGIN, send, push→очередь, завершение стрима."""

import asyncio
from typing import Any

import pytest

from app.messaging.max.protocol import (
    OP_CHAT_INFO,
    OP_GET_CONTACTS,
    OP_INIT,
    OP_LOGIN,
    OP_MSG_SEND,
    MaxAuthError,
)
from app.messaging.max.provider import MaxUserProvider
from app.messaging.types import SendResult
from app.models import Messenger


class FakeMaxClient:
    """Скриптованный клиент: request отвечает по opcode; push — вручную.

    ``scripted``: {opcode: payload} — ответ для обогащающих запросов
    (CHAT_INFO/GET_CONTACTS); отсутствующие opcode → пустой payload.
    """

    def __init__(self, *, login_error: Exception | None = None,
                 scripted: dict[int, dict] | None = None):
        self.requests: list[tuple[int, dict]] = []
        self._login_error = login_error
        self.scripted = scripted or {}
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
        if opcode in self.scripted:
            return {"cmd": 1, "seq": 0, "opcode": opcode,
                    "payload": self.scripted[opcode]}
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


def _light_push(chat_id: int, sender: int, msg_id: str, text: str) -> dict:
    """Лёгкий пуш (2-е+ сообщения чата): payload.message, без chat.type."""
    return {
        "opcode": 128,
        "payload": {
            "chatId": chat_id, "unread": 1,
            "message": {"sender": sender, "id": msg_id, "time": 1786906382424,
                        "text": text, "type": "USER", "attaches": []},
        },
    }


@pytest.mark.asyncio
async def test_light_push_enriches_name_and_checks_chat_type():
    """Лёгкий пуш: CHAT_INFO проверяет тип, GET_CONTACTS даёт имя/телефон

    (поймано живьём 2026-08-16: 2-е+ сообщения приходят как payload.message)."""
    fake = FakeMaxClient(scripted={
        OP_CHAT_INFO: {"chat": {"type": "DIALOG", "id": 53007183}},
        OP_GET_CONTACTS: {"contacts": [{
            "id": 349157962,
            "names": [{"name": "Тимур", "firstName": "Тимур", "type": "ONEME"}],
            "phones": [{"number": "+79990001122", "type": "MOBILE"}],
        }]},
    })
    provider = _make_provider(fake)
    await provider.connect()
    try:
        await fake._on_push(_light_push(53007183, 349157962, "m9", "Геор ты угадал"))
        msg = await asyncio.wait_for(provider._incoming_queue.get(), timeout=1)
        assert msg.sender_name == "Тимур"
        assert msg.sender_phone == "+79990001122"
        assert msg.external_message_id == "m9"
        # Обогащение спросило и тип чата, и контакт.
        ops = [op for op, _ in fake.requests]
        assert OP_CHAT_INFO in ops and OP_GET_CONTACTS in ops
        # Второе сообщение того же чата/отправителя — без новых запросов.
        n_req = len(fake.requests)
        await fake._on_push(_light_push(53007183, 349157962, "m10", "ещё"))
        msg2 = await asyncio.wait_for(provider._incoming_queue.get(), timeout=1)
        assert msg2.external_message_id == "m10"
        assert len(fake.requests) == n_req
    finally:
        await provider.disconnect()


@pytest.mark.asyncio
async def test_light_push_group_filtered_by_chat_info():
    fake = FakeMaxClient(scripted={
        OP_CHAT_INFO: {"chat": {"type": "GROUP", "id": 77}},
    })
    provider = _make_provider(fake)
    await provider.connect()
    try:
        await fake._on_push(_light_push(77, 349157962, "m11", "из группы"))
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(provider._incoming_queue.get(), timeout=0.1)
        assert provider._incoming_queue.empty()
    finally:
        await provider.disconnect()


@pytest.mark.asyncio
async def test_push_order_preserved_with_slow_enrichment():
    """Первое сообщение ждёт CHAT_INFO/GET_CONTACTS — второе не должно

    обогнать первое в incoming-очереди (воркер последовательный)."""
    fake = FakeMaxClient(scripted={
        OP_CHAT_INFO: {"chat": {"type": "DIALOG"}},
        OP_GET_CONTACTS: {"contacts": [{"names": [{"name": "Тимур"}]}]},
    })
    provider = _make_provider(fake)
    await provider.connect()
    try:
        await fake._on_push(_light_push(53007183, 349157962, "first", "1"))
        await fake._on_push(_light_push(53007183, 349157962, "second", "2"))
        m1 = await asyncio.wait_for(provider._incoming_queue.get(), timeout=1)
        m2 = await asyncio.wait_for(provider._incoming_queue.get(), timeout=1)
        assert (m1.external_message_id, m2.external_message_id) == ("first", "second")
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
