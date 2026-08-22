"""Юнит-тесты WhatsAppProvider: connect-контракт, разбор событий, эхо-фильтр,
квитанции exact-id, отправка/ошибки, resolve_peer, restriction → dead.

Фейки: FakeApi (scripted REST), FakeEvents (событийный транспорт),
настоящий WaMedia на MediaStorage(tmp_path) — реальной сети нет.
"""

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.media.storage import MediaStorage
from app.messaging.provider import SessionRevokedError
from app.messaging.resolve import ParsedDest
from app.messaging.types import ContentType
from app.messaging.whatsapp.api import WaAuthError, WaError
from app.messaging.whatsapp.media import WaMedia
from app.messaging.whatsapp.provider import WhatsAppProvider
from app.models import MessageDirection, Messenger

CHAT = "6281234567890@c.us"


class FakeApi:
    def __init__(self):
        self.sessions = {}
        self.started = None
        self.sent_texts = []
        self.sent_media = []
        self.contacts = {}
        self.downloads = {}
        self.fail_send: Exception | None = None

    async def get_session(self, sid):
        info = self.sessions[sid]
        if isinstance(info, Exception):
            raise info
        return info

    async def start_session(self, sid):
        self.started = sid
        self.sessions[sid] = {"status": "ready", "engineLoaded": True}

    async def send_text(self, sid, chat_id, text):
        if self.fail_send:
            raise self.fail_send
        self.sent_texts.append((chat_id, text))
        return {"messageId": f"m{len(self.sent_texts)}", "timestamp": 1}

    async def send_media(self, sid, chat_id, *, kind, b64, mimetype,
                         filename=None, caption=None):
        if self.fail_send:
            raise self.fail_send
        self.sent_media.append((chat_id, kind, b64, mimetype, filename, caption))
        return {"messageId": "mm1"}

    async def check_contact(self, sid, number):
        return self.contacts.get(number, {"exists": False, "whatsappId": None})

    async def download_media(self, sid, chat_id, message_id):
        return self.downloads.get(message_id, (b"data", "application/octet-stream"))

    async def aclose(self):
        pass


class FakeEvents:
    def __init__(self):
        self.started = None
        self.stopped = False

    async def start(self, sid):
        self.started = sid
        self.stopped = False

    async def stop(self):
        self.stopped = True

    def is_connected(self):
        return self.started is not None and not self.stopped


def make_provider(tmp_path, *, api=None, media=True, **kwargs):
    api = api or FakeApi()
    api.sessions.setdefault("s1", {"status": "ready", "engineLoaded": True})
    events = FakeEvents()
    wa_media = (
        WaMedia(api=api, storage=MediaStorage(tmp_path / "media", max_size_bytes=None))
        if media
        else None
    )
    provider = WhatsAppProvider(
        session_id="s1",
        api=api,
        media=wa_media,
        events_factory=lambda: events,
        **kwargs,
    )
    return provider, api, events


def event(name, data):
    return {"event": name, "sessionId": "s1", "data": data}


def received(**overrides):
    data = {
        "id": "m1",
        "from": CHAT,
        "to": "7000@c.us",
        "body": "привет",
        "type": "text",
        "timestamp": 1750000000,
        "kind": "individual",
        "hasMedia": False,
        "contact": {"name": "Иван Петров"},
    }
    data.update(overrides)
    return event("message.received", data)


async def next_incoming(provider, timeout=1.0):
    return await asyncio.wait_for(provider._incoming_queue.get(), timeout)


async def assert_no_incoming(provider, timeout=0.2):
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(provider._incoming_queue.get(), timeout)


# --- connect / disconnect ---


async def test_connect_ready_starts_events_and_workers(tmp_path):
    provider, _, events = make_provider(tmp_path)
    await provider.connect()
    assert events.started == "s1"
    assert provider.is_connected() is True
    await provider.disconnect()
    assert events.stopped is True
    assert await provider._incoming_queue.get() is None  # сентинел
    assert await provider._read_queue.get() is None


async def test_connect_starts_stopped_engine(tmp_path):
    provider, api, events = make_provider(tmp_path)
    api.sessions["s1"] = {"status": "created", "engineLoaded": False}
    await provider.connect()
    assert api.started == "s1"
    assert events.started == "s1"
    await provider.disconnect()


async def test_connect_failed_session_revoked(tmp_path):
    provider, api, _ = make_provider(tmp_path)
    api.sessions["s1"] = {"status": "failed", "lastError": "TOS_BLOCK"}
    with pytest.raises(SessionRevokedError):
        await provider.connect()


async def test_connect_tos_block_revoked(tmp_path):
    provider, api, _ = make_provider(tmp_path)
    api.sessions["s1"] = {
        "status": "disconnected",
        "restriction": {"kind": "tos_block", "code": "TOS_BLOCK"},
    }
    with pytest.raises(SessionRevokedError):
        await provider.connect()


# --- входящие ---


async def test_received_text_parses_fields(tmp_path):
    provider, _, _ = make_provider(tmp_path)
    await provider.connect()
    provider._on_event(received())
    msg = await next_incoming(provider)
    assert msg.messenger is Messenger.wa
    assert msg.external_chat_id == CHAT
    assert msg.sender_external_id == "6281234567890"
    assert msg.sender_phone == "6281234567890"
    assert msg.sender_name == "Иван Петров"
    assert msg.sender_first_name == "Иван"
    assert msg.sender_last_name == "Петров"
    assert msg.text == "привет"
    assert msg.direction is MessageDirection.inbound
    assert msg.timestamp == datetime.fromtimestamp(1750000000, tz=UTC)
    await provider.disconnect()


async def test_received_group_skipped(tmp_path):
    provider, _, _ = make_provider(tmp_path)
    await provider.connect()
    provider._on_event(received(kind="group", **{"from": "1203@g.us"}))
    await assert_no_incoming(provider)
    await provider.disconnect()


async def test_received_lid_uses_phone_identity(tmp_path):
    provider, _, _ = make_provider(tmp_path)
    await provider.connect()
    provider._on_event(received(**{"from": "999@lid", "senderPhone": "79160001122"}))
    msg = await next_incoming(provider)
    assert msg.external_chat_id == "999@lid"
    assert msg.sender_external_id == "79160001122"
    assert msg.sender_phone == "79160001122"
    await provider.disconnect()


async def test_received_unsupported_type_skipped(tmp_path):
    provider, _, _ = make_provider(tmp_path)
    await provider.connect()
    provider._on_event(received(type="location", location={"lat": 1}))
    await assert_no_incoming(provider)
    await provider.disconnect()


async def test_received_media_downloaded_to_storage(tmp_path):
    provider, api, _ = make_provider(tmp_path)
    api.downloads["m7"] = (b"jpegbytes", "image/jpeg")
    await provider.connect()
    provider._on_event(
        received(
            id="m7",
            type="image",
            body=None,
            hasMedia=True,
            media={"mimetype": "image/jpeg", "filename": "foto.jpg"},
        )
    )
    msg = await next_incoming(provider)
    assert msg.media is not None
    assert msg.media.file_name == "foto.jpg"
    assert msg.media.mime_type == "image/jpeg"
    assert msg.media.size == len(b"jpegbytes")
    stored = tmp_path / "media" / "in" / Path(msg.media.path).name
    assert stored.read_bytes() == b"jpegbytes"
    await provider.disconnect()


# --- эхо / device-outbound ---


async def test_rest_send_echo_filtered_device_sent_yields_outbound(tmp_path):
    provider, _, _ = make_provider(tmp_path)
    await provider.connect()
    result = await provider.send_message(CHAT, "вам", is_initiation=False)
    assert result.success and result.external_message_id == "m1"
    provider._on_event(
        event(
            "message.sent",
            {"id": "m1", "to": CHAT, "from": "7000@c.us", "type": "text", "body": "вам"},
        )
    )
    await assert_no_incoming(provider)  # эхо нашей отправки
    provider._on_event(
        event(
            "message.sent",
            {"id": "m9", "to": CHAT, "from": "7000@c.us", "type": "text", "body": "с телефона"},
        )
    )
    msg = await next_incoming(provider)
    assert msg.direction is MessageDirection.outbound
    assert msg.external_chat_id == CHAT
    await provider.disconnect()


# --- квитанции exact-id ---


async def test_ack_read_routes_to_known_chat(tmp_path):
    provider, _, _ = make_provider(tmp_path)
    await provider.connect()
    await provider.send_message(CHAT, "hi", is_initiation=False)
    provider._on_event(event("message.ack", {"id": "a1", "messageId": "m1", "status": "read"}))
    receipt = await asyncio.wait_for(provider._read_queue.get(), 1)
    assert receipt.external_chat_id == CHAT
    assert receipt.external_message_id == "m1"
    assert receipt.up_to_external_id is None
    await provider.disconnect()


async def test_ack_delivered_and_unknown_id_ignored(tmp_path):
    provider, _, _ = make_provider(tmp_path)
    await provider.connect()
    provider._on_event(event("message.ack", {"messageId": "m1", "status": "delivered"}))
    provider._on_event(event("message.ack", {"messageId": "zzz", "status": "read"}))
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(provider._read_queue.get(), 0.2)
    await provider.disconnect()


# --- отправка / ошибки ---


async def test_send_message_bad_chat_id(tmp_path):
    provider, _, _ = make_provider(tmp_path)
    await provider.connect()
    result = await provider.send_message("12345", "x", is_initiation=False)
    assert not result.success
    assert "bad_chat_id" in result.error
    await provider.disconnect()


async def test_send_throttle_maps_retry_after(tmp_path):
    provider, api, _ = make_provider(tmp_path)
    await provider.connect()
    api.fail_send = WaError("429", retryable=True, retry_after_sec=17)
    result = await provider.send_message(CHAT, "x", is_initiation=False)
    assert not result.success
    assert result.retry_after_seconds == 17
    await provider.disconnect()


async def test_send_auth_error_marks_dead(tmp_path):
    provider, api, _ = make_provider(tmp_path)
    await provider.connect()
    api.fail_send = WaAuthError()
    result = await provider.send_message(CHAT, "x", is_initiation=False)
    assert not result.success and result.error == "wa_auth"
    assert provider.is_dead() is True
    await provider.disconnect()


async def test_send_media_encodes_base64_with_kind(tmp_path):
    provider, api, _ = make_provider(tmp_path)
    await provider.connect()
    src = tmp_path / "pic.png"
    src.write_bytes(b"pngbytes")
    result = await provider.send_media(
        CHAT,
        src,
        ContentType.photo,
        mime_type="image/png",
        file_name="pic.png",
        caption="фото",
    )
    assert result.success and result.external_message_id == "mm1"
    chat, kind, b64, mime, name, caption = api.sent_media[0]
    assert (chat, kind) == (CHAT, "image")
    assert b64 == "cG5nYnl0ZXM="  # base64(b"pngbytes")
    assert (mime, name, caption) == ("image/png", "pic.png", "фото")
    await provider.disconnect()


async def test_resolve_peer_found_and_missing(tmp_path):
    provider, api, _ = make_provider(tmp_path)
    await provider.connect()
    api.contacts["79991234567"] = {"exists": True, "whatsappId": "79991234567@c.us"}
    peer = await provider.resolve_peer(ParsedDest("phone", "+79991234567"))
    assert peer is not None
    assert peer.external_user_id == "79991234567"
    assert peer.external_chat_id == "79991234567@c.us"
    assert peer.phone == "+79991234567"
    missing = await provider.resolve_peer(ParsedDest("phone", "+71110002233"))
    assert missing is None
    await provider.disconnect()


# --- restriction / supervise ---


async def test_sent_push_beating_rest_response_filtered_by_grace(tmp_path):
    """Пуш message.sent обогнал REST-ответ send_text: после грейса id уже
    зарегистрирован — НЕ ingested как device-outbound (паттерн MAX)."""
    provider, _, _ = make_provider(tmp_path)
    provider._echo_grace_sec = 0.15
    await provider.connect()

    send_task = asyncio.create_task(
        provider.send_message(CHAT, "го", is_initiation=False)
    )
    # Пуш успел прийти ДО возврата REST (id ещё не в _known_sends).
    provider._on_event(
        event(
            "message.sent",
            {"id": "m1", "to": CHAT, "from": "7000@c.us", "type": "text", "body": "го"},
        )
    )
    result = await send_task
    assert result.success and result.external_message_id == "m1"
    await assert_no_incoming(provider, timeout=0.5)  # грейс прикрыл окно
    await provider.disconnect()


async def test_restriction_event_tos_block_marks_dead(tmp_path):
    provider, _, _ = make_provider(tmp_path)
    await provider.connect()
    assert provider.restriction() is None
    provider._on_event(
        event(
            "session.restriction",
            {"sessionId": "s1", "active": True, "kind": "tos_block", "code": "TOS_BLOCK"},
        )
    )
    assert provider.is_dead() is True
    assert provider.restriction()["kind"] == "tos_block"
    provider._on_event(
        event("session.restriction", {"sessionId": "s1", "active": False, "kind": "tos_block"})
    )
    assert provider.restriction() is None
    await provider.disconnect()


async def test_supervise_poll_marks_dead_on_failed(tmp_path):
    provider, api, _ = make_provider(tmp_path)
    provider._status_poll_sec = 0.02
    await provider.connect()
    assert provider.is_dead() is False
    api.sessions["s1"] = {"status": "failed", "lastError": "boom"}
    for _ in range(50):
        if provider.is_dead():
            break
        await asyncio.sleep(0.02)
    assert provider.is_dead() is True
    await provider.disconnect()


async def test_photo_without_media_gets_placeholder_text(tmp_path):
    """Фото-событие без hasMedia/media (грабля 08-22: пустой пузырь в виджете):
    текст = плейсхолдер «[фото]», не None; с media-метой — скачивание идёт."""
    provider, api, _ = make_provider(tmp_path)
    await provider.connect()
    provider._on_event(received(type="image", body=None, hasMedia=False))
    msg = await next_incoming(provider)
    assert msg.content_type is ContentType.photo
    assert msg.text == "[фото]"
    assert msg.media is None
    # media-мета без hasMedia тоже триггерит скачивание
    api.downloads["m8"] = (b"jpg", "image/jpeg")
    provider._on_event(
        received(id="m8", type="image", body=None, hasMedia=False,
                 media={"mimetype": "image/jpeg"})
    )
    msg2 = await next_incoming(provider)
    assert msg2.media is not None
    await provider.disconnect()
