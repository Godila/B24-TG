import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telethon import events

from app.messaging.types import SendResult


@pytest.mark.asyncio
async def test_send_message_success():
    from app.messaging.telegram.provider import TelegramProvider

    provider = TelegramProvider(api_id=1, api_hash="x", sessions_dir="/tmp")
    mock_client = AsyncMock()
    mock_event = MagicMock()
    mock_event.id = 999
    mock_client.send_message.return_value = mock_event
    provider._client = mock_client  # type: ignore

    result = await provider.send_message(
        account_id=1, external_chat_id="12345", text="hello", is_initiation=False
    )
    assert isinstance(result, SendResult)
    assert result.success is True
    assert result.external_message_id == 999
    mock_client.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_send_message_floodwait():
    from telethon.errors import FloodWaitError

    from app.messaging.telegram.provider import TelegramProvider

    provider = TelegramProvider(api_id=1, api_hash="x", sessions_dir="/tmp")
    mock_client = AsyncMock()
    mock_client.send_message.side_effect = FloodWaitError(request=MagicMock(), capture=42)
    provider._client = mock_client  # type: ignore

    result = await provider.send_message(
        account_id=1, external_chat_id="12345", text="hello", is_initiation=True
    )
    assert result.success is False
    assert result.flood_wait_seconds == 42


def test_connect_registers_newmessage_incoming_builder():
    """connect() обязан регистрировать events.NewMessage(incoming=True):
    без builder Telethon передаёт сырые Update — inbound мёртв (баг)."""
    with patch("app.messaging.telegram.provider.TelegramClient") as mock_tl:
        client_inst = AsyncMock()
        client_inst.is_user_authorized = AsyncMock(return_value=True)
        mock_tl.return_value = client_inst

        from app.messaging.telegram.provider import TelegramProvider

        provider = TelegramProvider(api_id=1, api_hash="x", sessions_dir="/tmp")
        asyncio.run(provider.connect())

        client_inst.add_event_handler.assert_called_once()
        handler_arg, builder_arg = client_inst.add_event_handler.call_args[0]
        assert handler_arg == provider._on_new_message
        assert isinstance(builder_arg, events.NewMessage)
        # incoming=True: исходящие (свои) сообщения фильтруются.
        assert builder_arg.incoming is True


def test_on_new_message_builds_incoming_message():
    from app.messaging.telegram.provider import TelegramProvider

    provider = TelegramProvider(api_id=1, api_hash="x", sessions_dir="/tmp")

    sender = SimpleNamespace(
        id=4242,
        first_name="Иван",
        last_name=None,
        phone="+79990000000",
        username="ivan",
    )
    event = SimpleNamespace(
        chat_id=4242,
        is_reply=False,
        message=SimpleNamespace(message="Привет", id=777, date=None),
        get_sender=AsyncMock(return_value=sender),
    )

    asyncio.run(provider._on_new_message(event))
    msg = asyncio.run(provider._incoming_queue.get())
    assert msg.sender_tg_id == 4242
    assert msg.text == "Привет"
    assert msg.external_message_id == 777
    assert msg.external_chat_id == "4242"
    assert msg.account_id == 0  # перезапишет bootstrap.forward_incoming
