from unittest.mock import AsyncMock, MagicMock

import pytest

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
