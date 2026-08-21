"""Резолв «написать первым»: нормализация dest + resolve_peer TG/MAX."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon.tl.types import User

from app.messaging.max.protocol import MaxProtocolError
from app.messaging.max.provider import MaxUserProvider
from app.messaging.resolve import ParsedDest, ResolvedPeer, normalize_dest
from app.messaging.telegram.provider import TelegramProvider
from app.models import Messenger


# --------------------------------------------------------------------- #
# normalize_dest
# --------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("+79991234567", ("phone", "+79991234567")),
        ("79991234567", ("phone", "+79991234567")),
        ("89991234567", ("phone", "+79991234567")),  # RU-паста с 8
        ("+7 999 123-45-67", ("phone", "+79991234567")),
        ("8 (999) 123 45 67", ("phone", "+79991234567")),
        ("+441514960000", ("phone", "+441514960000")),
    ],
)
def test_normalize_phone(raw, expected):
    assert normalize_dest(Messenger.tg, raw) == ParsedDest(*expected)


@pytest.mark.parametrize("raw", ["12345", "+799912345678901234", "abc", "", "   "])
def test_normalize_garbage(raw):
    assert normalize_dest(Messenger.tg, raw) is None


def test_normalize_username():
    assert normalize_dest(Messenger.tg, "@Ivan_Petrov") == ParsedDest("username", "ivan_petrov")
    assert normalize_dest(Messenger.tg, "ivan_petrov") == ParsedDest("username", "ivan_petrov")


def test_normalize_username_max_rejected():
    """MAX ищет только по телефону — @username = None (роут ответит 422)."""
    assert normalize_dest(Messenger.max, "@ivan_petrov") is None
    assert normalize_dest(Messenger.max, "+79991234567") == ParsedDest("phone", "+79991234567")


# --------------------------------------------------------------------- #
# MaxUserProvider.resolve_peer
# --------------------------------------------------------------------- #
class _FakeMaxClient:
    """Минимальный клиент-шов для resolve_peer (без connect/push)."""

    closed = False

    def __init__(self, payload: dict | None = None, error: Exception | None = None):
        self._payload = payload
        self._error = error
        self.requests: list[tuple[int, dict]] = []

    def on_push(self, cb) -> None:  # провайдер регистрирует колбэк в конструкторе
        pass

    async def request(self, opcode: int, payload: dict | None = None, **_):
        self.requests.append((opcode, payload))
        if self._error is not None:
            raise self._error
        return {"cmd": 1, "seq": 0, "opcode": opcode, "payload": self._payload or {}}


def _max_provider(client: _FakeMaxClient, own_user_id: int | None = 401041669) -> MaxUserProvider:
    return MaxUserProvider(
        token="t",
        device_id="d",
        own_user_id=own_user_id,
        ws_url="wss://test",
        headers={},
        user_agent={"appVersion": "26.8.4"},
        client_factory=lambda: client,
    )


_OWN = 401041669
_PEER = 248843813  # прод-пара: chat 422733600 == own ^ peer


@pytest.mark.asyncio
async def test_max_resolve_phone_xor_chat_id():
    client = _FakeMaxClient(
        payload={"contact": {"id": _PEER, "names": [{"name": "Тимур", "type": "FULL_NAME"}]}}
    )
    peer = await _max_provider(client).resolve_peer(ParsedDest("phone", "+79990000000"))
    assert isinstance(peer, ResolvedPeer)
    assert peer.external_user_id == str(_PEER)
    assert peer.external_chat_id == str(_OWN ^ _PEER)
    assert peer.name == "Тимур"
    assert peer.phone == "+79990000000"
    assert client.requests == [(46, {"phone": "+79990000000"})]


@pytest.mark.asyncio
async def test_max_resolve_not_found_is_none():
    client = _FakeMaxClient(error=MaxProtocolError(46, {"error": "not.found"}))
    assert await _max_provider(client).resolve_peer(ParsedDest("phone", "+79991112222")) is None


@pytest.mark.asyncio
async def test_max_resolve_other_protocol_error_raises():
    client = _FakeMaxClient(error=MaxProtocolError(46, {"error": "boom"}))
    with pytest.raises(MaxProtocolError):
        await _max_provider(client).resolve_peer(ParsedDest("phone", "+79991112222"))


@pytest.mark.asyncio
async def test_max_resolve_garbage_contact_is_none():
    """Fail-closed: мусорный id (строка/пусто) → None, не успех с нулем."""
    for contact in ({"id": "abc"}, {}, None):
        client = _FakeMaxClient(payload={"contact": contact})
        assert await _max_provider(client).resolve_peer(ParsedDest("phone", "+79991112222")) is None


@pytest.mark.asyncio
async def test_max_resolve_username_unsupported():
    client = _FakeMaxClient()
    assert await _max_provider(client).resolve_peer(ParsedDest("username", "x")) is None
    assert client.requests == []  # в сеть не ходили


@pytest.mark.asyncio
async def test_max_resolve_requires_own_user_id():
    with pytest.raises(RuntimeError):
        await _max_provider(_FakeMaxClient(), own_user_id=None).resolve_peer(
            ParsedDest("phone", "+79991112222")
        )


# --------------------------------------------------------------------- #
# TelegramProvider.resolve_peer
# --------------------------------------------------------------------- #
def _tg_provider(client: AsyncMock) -> TelegramProvider:
    provider = TelegramProvider(api_id=1, api_hash="x", sessions_dir="/tmp")
    provider._client = client  # type: ignore[assignment]
    return provider


def _tg_user(**kw) -> User:
    base = {"id": 777, "first_name": "Иван", "last_name": None, "username": None, "phone": None}
    base.update(kw)
    return User(**base)


@pytest.mark.asyncio
async def test_tg_resolve_username():
    client = AsyncMock()
    client.get_entity = AsyncMock(return_value=_tg_user(username="ivan_p"))
    peer = await _tg_provider(client).resolve_peer(ParsedDest("username", "ivan_p"))
    assert peer.external_user_id == "777"
    assert peer.external_chat_id == "777"  # приватный чат TG == id клиента
    assert peer.username == "ivan_p"
    client.get_entity.assert_awaited_once_with("ivan_p")


@pytest.mark.asyncio
async def test_tg_resolve_phone_via_import_contacts():
    client = AsyncMock()
    client.return_value = SimpleNamespace(users=[_tg_user(phone="+79991234567")])
    peer = await _tg_provider(client).resolve_peer(ParsedDest("phone", "+79991234567"))
    assert peer.external_user_id == "777"
    assert peer.phone == "+79991234567"
    # Вызов шёл через ImportContactsRequest с нашим номером.
    (request,) = client.call_args[0]
    assert request.contacts[0].phone == "+79991234567"


@pytest.mark.asyncio
async def test_tg_resolve_phone_not_in_telegram():
    client = AsyncMock()
    client.return_value = SimpleNamespace(users=[])
    assert await _tg_provider(client).resolve_peer(ParsedDest("phone", "+79990000000")) is None


@pytest.mark.asyncio
async def test_tg_resolve_not_user_entity_is_none():
    """Канал/группа по @username — не собеседник личного диалога."""
    client = AsyncMock()
    client.get_entity = AsyncMock(return_value=object())
    assert await _tg_provider(client).resolve_peer(ParsedDest("username", "channel")) is None


@pytest.mark.asyncio
async def test_tg_resolve_not_connected_raises():
    provider = TelegramProvider(api_id=1, api_hash="x", sessions_dir="/tmp")
    with pytest.raises(ConnectionError):
        await provider.resolve_peer(ParsedDest("username", "ivan_p"))
