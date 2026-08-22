"""Юнит-тесты WaMedia: сохранение входящих/device-outbound в MediaStorage
(контракт направления тома), выбор расширения, проброс ошибок."""

from app.media.storage import MediaStorage
from app.messaging.whatsapp.api import WaError
from app.messaging.whatsapp.media import WaMedia


class StubApi:
    def __init__(self, payload=b"data", ctype="image/png"):
        self._payload = payload
        self._ctype = ctype

    async def download_media(self, session_id, chat_id, message_id):
        return self._payload, self._ctype


async def test_download_out_direction_uses_filename_suffix(tmp_path):
    media = WaMedia(api=StubApi(b"abc"), storage=MediaStorage(tmp_path, max_size_bytes=None))
    payload = await media.download(
        session_id="s1",
        chat_id="628@c.us",
        message_id="m1",
        mimetype="image/jpeg",
        file_name="foto.jpg",
        direction="out",
    )
    assert payload.file_name == "foto.jpg"
    assert payload.mime_type == "image/jpeg"
    assert payload.size == 3
    assert payload.path.endswith(".jpg")
    # Контракт тома: device-outbound живёт в out/, не в in/
    assert payload.path.startswith("out/")
    assert (tmp_path / payload.path).read_bytes() == b"abc"


async def test_download_default_in_direction(tmp_path):
    media = WaMedia(api=StubApi(b"abc"), storage=MediaStorage(tmp_path, max_size_bytes=None))
    payload = await media.download(
        session_id="s1", chat_id="628@c.us", message_id="m1", mimetype=None, file_name=None
    )
    assert payload.mime_type == "image/png"
    assert payload.path.endswith(".png")
    assert payload.path.startswith("in/")


async def test_download_api_error_propagates(tmp_path):
    class FailingApi(StubApi):
        async def download_media(self, session_id, chat_id, message_id):
            raise WaError("openwa 404", retryable=False)

    media = WaMedia(api=FailingApi(), storage=MediaStorage(tmp_path, max_size_bytes=None))
    try:
        await media.download(
            session_id="s1", chat_id="628@c.us", message_id="m1", mimetype=None, file_name=None
        )
        raised = False
    except WaError:
        raised = True
    assert raised
