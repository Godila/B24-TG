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
        external_chat_id="12345", text="hello", is_initiation=False
    )
    assert isinstance(result, SendResult)
    assert result.success is True
    assert result.external_message_id == "999"
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
        external_chat_id="12345", text="hello", is_initiation=True
    )
    assert result.success is False
    assert result.retry_after_seconds == 42


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
        is_private=True,
        is_reply=False,
        message=SimpleNamespace(message="Привет", id=777, date=None),
        get_sender=AsyncMock(return_value=sender),
    )

    asyncio.run(provider._on_new_message(event))
    msg = asyncio.run(provider._incoming_queue.get())
    assert msg.sender_external_id == "4242"
    assert msg.messenger.value == "tg"
    assert msg.text == "Привет"
    assert msg.content_type.value == "text"
    assert msg.external_message_id == "777"
    assert msg.external_chat_id == "4242"


def test_on_new_message_group_chat_skipped():
    """Группы/каналы не инжестятся: иначе контакт+сделка в CRM и ответ
    менеджера уходили бы в группу (MAX фильтрует так же через CHAT_INFO)."""
    from app.messaging.telegram.provider import TelegramProvider

    provider = TelegramProvider(api_id=1, api_hash="x", sessions_dir="/tmp")

    event = SimpleNamespace(
        chat_id=-1001234567890,
        is_private=False,
        is_reply=False,
        message=SimpleNamespace(message="из группы", id=778, date=None),
        get_sender=AsyncMock(return_value=SimpleNamespace(id=1)),
    )

    asyncio.run(provider._on_new_message(event))
    assert provider._incoming_queue.empty()


def _tg_message(text, media):
    """Псевдо-Message Telethon: .message, .media — как в events.NewMessage."""
    return SimpleNamespace(message=text, media=media)


def _doc(*attributes):
    from telethon.tl import types as tl

    return tl.Document(
        id=1,
        access_hash=1,
        file_reference=b"",
        date=None,
        mime_type="application/octet-stream",
        size=100,
        dc_id=1,
        attributes=list(attributes),
    )


def test_content_type_text_without_media():
    from app.messaging.telegram.provider import TelegramProvider

    ctype, text = TelegramProvider._content_type_and_text(
        _tg_message("Привет", media=None)
    )
    assert ctype.value == "text"
    assert text == "Привет"


def test_content_type_photo_without_caption():
    from telethon.tl import types as tl

    from app.messaging.telegram.provider import TelegramProvider

    ctype, text = TelegramProvider._content_type_and_text(
        _tg_message("", media=tl.MessageMediaPhoto(photo=None))
    )
    assert ctype.value == "photo"
    assert text == "[фото]"


def test_content_type_photo_caption_preserved():
    from telethon.tl import types as tl

    from app.messaging.telegram.provider import TelegramProvider

    ctype, text = TelegramProvider._content_type_and_text(
        _tg_message("Смотрите чертеж", media=tl.MessageMediaPhoto(photo=None))
    )
    assert ctype.value == "photo"
    assert text == "Смотрите чертеж"


def test_content_type_voice_video_sticker_file():
    from telethon.tl import types as tl

    from app.messaging.telegram.provider import TelegramProvider

    def check(doc, expected_type, expected_text):
        media = tl.MessageMediaDocument(document=doc)
        ctype, text = TelegramProvider._content_type_and_text(
            _tg_message("", media=media)
        )
        assert ctype.value == expected_type
        assert text == expected_text

    check(
        _doc(tl.DocumentAttributeAudio(duration=5, voice=True)),
        "voice",
        "[голосовое сообщение]",
    )
    check(
        _doc(tl.DocumentAttributeVideo(duration=5, w=640, h=480)),
        "video",
        "[видео]",
    )
    check(
        _doc(
            tl.DocumentAttributeSticker(
                alt="🙂", stickerset=tl.InputStickerSetEmpty()
            )
        ),
        "sticker",
        "[стикер]",
    )
    check(
        _doc(tl.DocumentAttributeFilename(file_name="doc.pdf")),
        "file",
        "[файл]",
    )


def test_content_type_unknown_media_fallback():
    from telethon.tl import types as tl

    from app.messaging.telegram.provider import TelegramProvider

    ctype, text = TelegramProvider._content_type_and_text(
        _tg_message(None, media=tl.MessageMediaGeo(geo=tl.GeoPointEmpty()))
    )
    assert ctype.value == "file"
    assert text == "[вложение]"


def test_on_new_message_media_becomes_placeholder():
    """Медиа без подписи не теряется: в очереди плейсхолдер + верный тип."""
    from telethon.tl import types as tl

    from app.messaging.telegram.provider import TelegramProvider

    provider = TelegramProvider(api_id=1, api_hash="x", sessions_dir="/tmp")

    sender = SimpleNamespace(id=4242, first_name="Иван")
    event = SimpleNamespace(
        chat_id=4242,
        is_private=True,
        is_reply=False,
        message=SimpleNamespace(
            message="",
            media=tl.MessageMediaPhoto(photo=None),
            id=778,
            date=None,
        ),
        get_sender=AsyncMock(return_value=sender),
    )

    asyncio.run(provider._on_new_message(event))
    msg = asyncio.run(provider._incoming_queue.get())
    assert msg.content_type.value == "photo"
    assert msg.text == "[фото]"


def test_connect_unauthorized_session_raises_revoked():
    """Инвалидированная .session — терминальная SessionRevokedError

    (не RuntimeError): AccountSyncWorker по ней гасит аккаунт в offline
    и алертит «переподключите по QR», вместо ретраев «сетевого сбоя»."""
    with patch("app.messaging.telegram.provider.TelegramClient") as mock_tl:
        client_inst = AsyncMock()
        client_inst.is_user_authorized = AsyncMock(return_value=False)
        mock_tl.return_value = client_inst

        from app.messaging.provider import SessionRevokedError
        from app.messaging.telegram.provider import TelegramProvider

        provider = TelegramProvider(api_id=1, api_hash="x", sessions_dir="/tmp")
        with pytest.raises(SessionRevokedError):
            asyncio.run(provider.connect())
        client_inst.disconnect.assert_awaited_once()
