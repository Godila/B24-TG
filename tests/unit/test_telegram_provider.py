import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telethon import events

from app.messaging.types import ContentType, SendResult


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

    result = await provider.send_message(external_chat_id="12345", text="hello", is_initiation=True)
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

    ctype, text = TelegramProvider._content_type_and_text(_tg_message("Привет", media=None))
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
        ctype, text = TelegramProvider._content_type_and_text(_tg_message("", media=media))
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
        _doc(tl.DocumentAttributeSticker(alt="🙂", stickerset=tl.InputStickerSetEmpty())),
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


# --- Медиа: метаданные и скачивание (входящие) ---


def test_media_meta_photo_is_jpeg():
    from telethon.tl import types as tl

    from app.messaging.telegram.provider import TelegramProvider

    assert TelegramProvider._media_meta(
        SimpleNamespace(media=tl.MessageMediaPhoto(photo=None))
    ) == ("image/jpeg", None, None)


def test_media_meta_document():
    from telethon.tl import types as tl

    from app.messaging.telegram.provider import TelegramProvider

    doc = tl.Document(
        id=1,
        access_hash=1,
        file_reference=b"",
        date=None,
        mime_type="application/pdf",
        size=1234,
        dc_id=1,
        attributes=[tl.DocumentAttributeFilename(file_name="report.pdf")],
    )
    meta = TelegramProvider._media_meta(
        SimpleNamespace(media=tl.MessageMediaDocument(document=doc))
    )
    assert meta == ("application/pdf", 1234, "report.pdf")


def test_media_meta_non_media():
    from app.messaging.telegram.provider import TelegramProvider

    assert TelegramProvider._media_meta(SimpleNamespace(media=None)) == (None, None, None)


@pytest.mark.asyncio
async def test_download_media_saves_and_returns_payload(tmp_path):
    from pathlib import Path

    from telethon.tl import types as tl

    from app.media.storage import MediaStorage
    from app.messaging.telegram.provider import TelegramProvider

    provider = TelegramProvider(
        api_id=1,
        api_hash="x",
        sessions_dir="/tmp",
        media_storage=MediaStorage(tmp_path, max_size_bytes=1000),
    )

    async def fake_download(message, file=None):
        Path(file).write_bytes(b"JPEGDATA")
        return file

    provider._client = SimpleNamespace(download_media=fake_download)  # type: ignore
    payload = await provider._download_media(
        SimpleNamespace(media=tl.MessageMediaPhoto(photo=None))
    )
    assert payload is not None
    assert payload.path.startswith("in/")
    assert payload.path.endswith(".jpg")
    assert payload.mime_type == "image/jpeg"
    assert payload.size == 8
    assert MediaStorage(tmp_path).abs_path(payload.path).read_bytes() == b"JPEGDATA"


@pytest.mark.asyncio
async def test_download_media_skips_oversize_declared(tmp_path):
    from telethon.tl import types as tl

    from app.media.storage import MediaStorage
    from app.messaging.telegram.provider import TelegramProvider

    provider = TelegramProvider(
        api_id=1,
        api_hash="x",
        sessions_dir="/tmp",
        media_storage=MediaStorage(tmp_path, max_size_bytes=10),
    )
    calls = []

    async def fake_download(message, file=None):
        calls.append(file)
        return file

    provider._client = SimpleNamespace(download_media=fake_download)  # type: ignore
    doc = tl.Document(
        id=1,
        access_hash=1,
        file_reference=b"",
        date=None,
        mime_type="video/mp4",
        size=100,
        dc_id=1,
        attributes=[],
    )
    payload = await provider._download_media(
        SimpleNamespace(media=tl.MessageMediaDocument(document=doc))
    )
    assert payload is None
    assert calls == []  # даже не начинаем качать


@pytest.mark.asyncio
async def test_download_media_actual_oversize_removes_file(tmp_path):
    from pathlib import Path

    from telethon.tl import types as tl

    from app.media.storage import MediaStorage
    from app.messaging.telegram.provider import TelegramProvider

    provider = TelegramProvider(
        api_id=1,
        api_hash="x",
        sessions_dir="/tmp",
        media_storage=MediaStorage(tmp_path, max_size_bytes=5),
    )
    written = []

    async def fake_download(message, file=None):
        Path(file).write_bytes(b"0123456789")  # 10 байт — больше лимита
        written.append(Path(file))
        return file

    provider._client = SimpleNamespace(download_media=fake_download)  # type: ignore
    payload = await provider._download_media(
        SimpleNamespace(media=tl.MessageMediaPhoto(photo=None))
    )
    assert payload is None
    assert not written[0].exists()  # файл удалён, строки не будет


@pytest.mark.asyncio
async def test_download_media_failure_returns_none(tmp_path):
    from telethon.tl import types as tl

    from app.media.storage import MediaStorage
    from app.messaging.telegram.provider import TelegramProvider

    provider = TelegramProvider(
        api_id=1,
        api_hash="x",
        sessions_dir="/tmp",
        media_storage=MediaStorage(tmp_path),
    )

    async def broken_download(message, file=None):
        raise RuntimeError("network gone")

    provider._client = SimpleNamespace(download_media=broken_download)  # type: ignore
    payload = await provider._download_media(
        SimpleNamespace(media=tl.MessageMediaPhoto(photo=None))
    )
    # Сообщение не теряется: медиа нет, текст-плейсхолдер остаётся.
    assert payload is None


def test_on_new_message_downloads_media(tmp_path):
    from pathlib import Path

    from telethon.tl import types as tl

    from app.media.storage import MediaStorage
    from app.messaging.telegram.provider import TelegramProvider

    provider = TelegramProvider(
        api_id=1,
        api_hash="x",
        sessions_dir="/tmp",
        media_storage=MediaStorage(tmp_path),
    )

    async def fake_download(message, file=None):
        Path(file).write_bytes(b"IMG")
        return file

    provider._client = SimpleNamespace(download_media=fake_download)  # type: ignore
    sender = SimpleNamespace(id=4242, first_name="Иван")
    event = SimpleNamespace(
        chat_id=4242,
        is_private=True,
        is_reply=False,
        message=SimpleNamespace(
            message="вот фото",
            media=tl.MessageMediaPhoto(photo=None),
            id=779,
            date=None,
        ),
        get_sender=AsyncMock(return_value=sender),
    )

    asyncio.run(provider._on_new_message(event))
    msg = asyncio.run(provider._incoming_queue.get())
    assert msg.content_type.value == "photo"
    assert msg.text == "вот фото"  # caption сохранён
    assert msg.media is not None
    assert msg.media.path.startswith("in/")
    assert msg.media.mime_type == "image/jpeg"
    assert msg.media.size == 3


def test_on_new_message_sticker_not_downloaded(tmp_path):
    from telethon.tl import types as tl

    from app.media.storage import MediaStorage
    from app.messaging.telegram.provider import TelegramProvider

    provider = TelegramProvider(
        api_id=1,
        api_hash="x",
        sessions_dir="/tmp",
        media_storage=MediaStorage(tmp_path),
    )
    calls = []

    async def fake_download(message, file=None):
        calls.append(file)
        return file

    provider._client = SimpleNamespace(download_media=fake_download)  # type: ignore
    sender = SimpleNamespace(id=4242, first_name="Иван")
    event = SimpleNamespace(
        chat_id=4242,
        is_private=True,
        is_reply=False,
        message=SimpleNamespace(
            message="",
            media=tl.MessageMediaDocument(
                document=_doc(
                    tl.DocumentAttributeSticker(alt=":)", stickerset=tl.InputStickerSetEmpty())
                )
            ),
            id=780,
            date=None,
        ),
        get_sender=AsyncMock(return_value=sender),
    )

    asyncio.run(provider._on_new_message(event))
    msg = asyncio.run(provider._incoming_queue.get())
    assert msg.content_type.value == "sticker"
    assert msg.text == "[стикер]"
    assert msg.media is None
    assert calls == []


def test_on_new_message_without_storage_media_is_none():
    """Storage не передан (тесты/онбординг) — прежнее поведение:
    плейсхолдер в тексте, media=None."""
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
            id=781,
            date=None,
        ),
        get_sender=AsyncMock(return_value=sender),
    )

    asyncio.run(provider._on_new_message(event))
    msg = asyncio.run(provider._incoming_queue.get())
    assert msg.media is None
    assert msg.text == "[фото]"


# --- Медиа: отправка (send_media) ---


@pytest.mark.asyncio
async def test_send_media_success(tmp_path):
    from app.messaging.telegram.provider import TelegramProvider

    provider = TelegramProvider(api_id=1, api_hash="x", sessions_dir="/tmp")
    mock_client = AsyncMock()
    mock_client.send_file.return_value = SimpleNamespace(id=555)
    provider._client = mock_client  # type: ignore

    src = tmp_path / "photo.jpg"
    src.write_bytes(b"img")
    result = await provider.send_media(
        "12345",
        src,
        ContentType("photo"),
        mime_type="image/jpeg",
        file_name="photo.jpg",
        caption="смотрите",
    )
    assert result.success is True
    assert result.external_message_id == "555"
    args, kwargs = mock_client.send_file.await_args
    assert args == (12345, str(src))
    assert kwargs["caption"] == "смотрите"
    assert kwargs["voice_note"] is False
    # Оригинальное имя документа: на томе файл лежит под uuid-именем, без
    # атрибута клиент увидел бы хекс вместо «photo.jpg».
    from telethon.tl.types import DocumentAttributeFilename

    fn = [a for a in kwargs["attributes"] if isinstance(a, DocumentAttributeFilename)]
    assert len(fn) == 1 and fn[0].file_name == "photo.jpg"


@pytest.mark.asyncio
async def test_send_media_voice_note(tmp_path):
    from app.messaging.telegram.provider import TelegramProvider

    provider = TelegramProvider(api_id=1, api_hash="x", sessions_dir="/tmp")
    mock_client = AsyncMock()
    mock_client.send_file.return_value = SimpleNamespace(id=556)
    provider._client = mock_client  # type: ignore

    src = tmp_path / "voice.oga"
    src.write_bytes(b"ogg")
    await provider.send_media("12345", src, ContentType("voice"))
    assert mock_client.send_file.await_args.kwargs["voice_note"] is True


@pytest.mark.asyncio
async def test_send_media_empty_caption_becomes_none(tmp_path):
    from app.messaging.telegram.provider import TelegramProvider

    provider = TelegramProvider(api_id=1, api_hash="x", sessions_dir="/tmp")
    mock_client = AsyncMock()
    mock_client.send_file.return_value = SimpleNamespace(id=557)
    provider._client = mock_client  # type: ignore

    src = tmp_path / "doc.pdf"
    src.write_bytes(b"pdf")
    await provider.send_media("12345", src, ContentType("file"), caption="")
    assert mock_client.send_file.await_args.kwargs["caption"] is None


@pytest.mark.asyncio
async def test_send_media_floodwait(tmp_path):
    from telethon.errors import FloodWaitError

    from app.messaging.telegram.provider import TelegramProvider

    provider = TelegramProvider(api_id=1, api_hash="x", sessions_dir="/tmp")
    mock_client = AsyncMock()
    mock_client.send_file.side_effect = FloodWaitError(request=MagicMock(), capture=33)
    provider._client = mock_client  # type: ignore

    src = tmp_path / "doc.pdf"
    src.write_bytes(b"pdf")
    result = await provider.send_media("12345", src, ContentType("file"))
    assert result.success is False
    assert result.retry_after_seconds == 33


@pytest.mark.asyncio
async def test_send_media_not_connected(tmp_path):
    from app.messaging.telegram.provider import TelegramProvider

    provider = TelegramProvider(api_id=1, api_hash="x", sessions_dir="/tmp")
    src = tmp_path / "doc.pdf"
    src.write_bytes(b"pdf")
    result = await provider.send_media("12345", src, ContentType("file"))
    assert result.success is False
    assert result.error == "not connected"


def test_supports_media_flags():
    from app.messaging.telegram.provider import TelegramProvider

    assert TelegramProvider(api_id=1, api_hash="x", sessions_dir="/tmp").supports_media()


@pytest.mark.asyncio
async def test_download_media_follows_telethon_path_change(tmp_path):
    """Telethon дописывает расширение к пути без него (webpage-превью,
    контакт) — в MediaPayload должен попасть РЕАЛЬНЫЙ путь файла, иначе
    строка БД указывает на несуществующий файл (вечный 404 раздачи)."""
    from pathlib import Path

    from app.media.storage import MediaStorage
    from app.messaging.telegram.provider import TelegramProvider

    provider = TelegramProvider(
        api_id=1,
        api_hash="x",
        sessions_dir="/tmp",
        media_storage=MediaStorage(tmp_path),
    )

    async def fake_download(message, file=None):
        shifted = Path(str(file) + ".jpg")  # telethon-style: ext добавлен
        shifted.write_bytes(b"IMG")
        return str(shifted)

    provider._client = SimpleNamespace(download_media=fake_download)  # type: ignore
    # media без мета (webpage) → расширения нет → telethon допишет .jpg
    payload = await provider._download_media(SimpleNamespace(media=None))
    assert payload is not None
    assert payload.path.startswith("in/")
    assert payload.path.endswith(".jpg")
    assert MediaStorage(tmp_path).abs_path(payload.path).read_bytes() == b"IMG"
