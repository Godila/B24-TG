"""max/media.py: UploadWaiter, толерантные экстракторы, upload/download.

Сеть полностью исключена: WS — скриптованный колбэк, HTTP — httpx.MockTransport
(шов http_factory провайдера). Формы ответов — по реверс-моделям комьюнити
(PyMax/vkmax), живой смоук может их уточнить — экстракторы для того и изолированы.
"""

import asyncio

import httpx
import pytest

from app.media.storage import MediaStorage
from app.messaging.max.media import (
    MaxMediaClient,
    MaxMediaError,
    UploadWaiter,
    fallback_mime,
    find_upload_keys,
    pick_video_url,
)
from app.messaging.max.protocol import (
    OP_FILE_GET,
    OP_FILE_UPLOAD,
    OP_PHOTO_UPLOAD,
    OP_UPLOAD_NOTIFY,
    OP_VIDEO_GET,
    OP_VIDEO_UPLOAD,
    MaxThrottleError,
)
from app.messaging.max.push_parser import MaxAttach

PHOTO_BYTES = b"JPEGBYTES-123"


class FakeWs:
    """Скриптованный WS-запрос: {opcode: payload} + журнал вызовов."""

    def __init__(self, scripted: dict[int, dict] | None = None, errors: dict[int, Exception] | None = None):
        self.calls: list[tuple[int, dict]] = []
        self.scripted = scripted or {}
        self.errors = errors or {}

    async def __call__(self, opcode: int, payload: dict | None = None, *, timeout=None) -> dict:
        self.calls.append((opcode, payload or {}))
        if opcode in self.errors:
            raise self.errors[opcode]
        return {"cmd": 1, "seq": 1, "opcode": opcode, "payload": self.scripted.get(opcode, {})}


def _client(tmp_path, ws, waiter=None, handler=None, *, storage=None, limit=None, ready_timeout=5.0):
    if storage is None:
        storage = MediaStorage(tmp_path / "media", max_size_bytes=limit)
    return MaxMediaClient(
        storage=storage,
        ws_request=ws,
        waiter=waiter or UploadWaiter(),
        headers={"Origin": "https://web.max.ru"},
        upload_ready_timeout_sec=ready_timeout,
        http_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def _assert_incoming_empty(storage: MediaStorage) -> None:
    """in/ пуст (частичных файлов не осталось)."""
    in_dir = storage.root / "in"
    assert not (in_dir.exists() and list(in_dir.iterdir()))


def _ok_json(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={})


# ---------------------------------------------------------------------- #
# UploadWaiter / find_upload_keys
# ---------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_waiter_feed_resolves_matching_future():
    waiter = UploadWaiter()
    fut = waiter.expect("file", 5)
    waiter.feed({"result": {"fileId": 5}})
    assert await asyncio.wait_for(fut, 0.1) is True
    assert waiter._pending == {}


@pytest.mark.asyncio
async def test_waiter_feed_ignores_foreign_ids():
    waiter = UploadWaiter()
    fut = waiter.expect("file", 5)
    waiter.feed({"fileId": 6})
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(fut, 0.05)
    waiter.abandon("file", 5)


@pytest.mark.asyncio
async def test_waiter_abandon_cleans_registry():
    waiter = UploadWaiter()
    waiter.expect("file", 5)
    waiter.abandon("file", 5)
    waiter.feed({"fileId": 5})  # некого будить — не бросает
    assert waiter._pending == {}


@pytest.mark.asyncio
async def test_waiter_fail_all_wakes_with_exception():
    waiter = UploadWaiter()
    fut = waiter.expect("video", 7)
    waiter.fail_all(ConnectionError("max ws closed"))
    with pytest.raises(ConnectionError):
        await fut


def test_find_upload_keys_nested_and_typed():
    payload = {"a": [{"fileId": 5}, {"videoId": "7"}], "b": {"c": {"videoId": 9}}}
    assert find_upload_keys(payload) == {("file", 5), ("video", 7), ("video", 9)}


def test_find_upload_keys_garbage_is_empty():
    assert find_upload_keys(None) == set()
    assert find_upload_keys("строка") == set()
    assert find_upload_keys({"fileId": "не-цифра"}) == set()


# ---------------------------------------------------------------------- #
# Выбор качества видео / mime-фолбэки
# ---------------------------------------------------------------------- #
def test_pick_video_url_prefers_720_over_360():
    payload = {"MP4_360": "https://x/360", "MP4_720": "https://x/720"}
    assert pick_video_url(payload) == "https://x/720"


def test_pick_video_url_dict_value_and_any_mp4():
    payload = {"MP4_240": {"url": "https://x/240"}}
    assert pick_video_url(payload) == "https://x/240"


def test_pick_video_url_ignores_external():
    payload = {"EXTERNAL": "https://youtube/...", "dynamicUrl": "https://dyn/1"}
    assert pick_video_url(payload) is None


def test_fallback_mime_by_kind_and_name():
    assert fallback_mime(MaxAttach(kind="PHOTO")) == "image/jpeg"
    assert fallback_mime(MaxAttach(kind="VIDEO")) == "video/mp4"
    assert fallback_mime(MaxAttach(kind="AUDIO")) == "audio/ogg"
    assert fallback_mime(MaxAttach(kind="FILE", file_name="a.pdf")) == "application/pdf"
    assert fallback_mime(MaxAttach(kind="FILE")) is None


# ---------------------------------------------------------------------- #
# Upload
# ---------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_upload_photo_happy_path(tmp_path):
    ws = FakeWs(scripted={OP_PHOTO_UPLOAD: {"url": "https://iu.oneme.ru/up?k=1"}})
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["content_type"] = request.headers.get("content-type", "")
        seen["has_file_field"] = b'name="file"' in request.content
        seen["has_name"] = b"photo.jpg" in request.content
        return httpx.Response(200, json={"photos": {"11": {"token": "tok1"}}})

    path = tmp_path / "photo.jpg"
    path.write_bytes(PHOTO_BYTES)
    client = _client(tmp_path, ws, handler=handler)
    try:
        attaches = await client.upload(
            chat_id=42, kind="PHOTO", path=path, mime="image/jpeg", file_name="photo.jpg"
        )
    finally:
        await client.aclose()
    assert attaches == [{"_type": "PHOTO", "photoToken": "tok1"}]
    assert seen["url"].startswith("https://iu.oneme.ru/up")
    assert seen["content_type"].startswith("multipart/form-data")
    assert seen["has_file_field"] and seen["has_name"]
    # Фото не требует op 65 (notify шлётся только FILE/VIDEO).
    assert [op for op, _ in ws.calls] == [OP_PHOTO_UPLOAD]


@pytest.mark.asyncio
async def test_upload_file_waits_136_push(tmp_path):
    ws = FakeWs(
        scripted={
            OP_FILE_UPLOAD: {"info": [{"url": "https://fu.oneme.ru/up", "fileId": 55, "token": "t"}]}
        }
    )
    waiter = UploadWaiter()

    def handler(request: httpx.Request) -> httpx.Response:
        # 136 приходит сразу после POST — регистрация до POST обязана
        # переживать эту гонку.
        asyncio.get_event_loop().call_soon(lambda: waiter.feed({"fileId": 55}))
        return httpx.Response(200, json={})

    path = tmp_path / "doc.pdf"
    path.write_bytes(b"%PDF-1.4 fake")
    client = _client(tmp_path, ws, waiter=waiter, handler=handler)
    try:
        attaches = await client.upload(
            chat_id=42, kind="FILE", path=path, mime="application/pdf", file_name="doc.pdf"
        )
    finally:
        await client.aclose()
    assert attaches == [{"_type": "FILE", "fileId": 55}]
    ops = [op for op, _ in ws.calls]
    assert ops == [OP_UPLOAD_NOTIFY, OP_FILE_UPLOAD]
    notify_payload = ws.calls[0][1]
    assert notify_payload == {"chatId": 42, "type": "FILE"}


@pytest.mark.asyncio
async def test_upload_video_keeps_token(tmp_path):
    ws = FakeWs(
        scripted={
            OP_VIDEO_UPLOAD: {"info": [{"url": "https://vu.okcdn.ru/up", "videoId": 77, "token": "vt"}]}
        }
    )
    waiter = UploadWaiter()

    def handler(request: httpx.Request) -> httpx.Response:
        asyncio.get_event_loop().call_soon(lambda: waiter.feed({"videoId": 77}))
        return httpx.Response(200, json={})

    path = tmp_path / "clip.mp4"
    path.write_bytes(b"MP4DATA")
    client = _client(tmp_path, ws, waiter=waiter, handler=handler)
    try:
        attaches = await client.upload(
            chat_id=42, kind="VIDEO", path=path, mime="video/mp4", file_name="clip.mp4"
        )
    finally:
        await client.aclose()
    assert attaches == [{"_type": "VIDEO", "videoId": 77, "token": "vt"}]


@pytest.mark.asyncio
async def test_upload_136_timeout_raises_and_cleans(tmp_path):
    ws = FakeWs(scripted={OP_FILE_UPLOAD: {"info": [{"url": "https://fu/up", "fileId": 5}]}})
    waiter = UploadWaiter()
    client = _client(tmp_path, ws, waiter=waiter, handler=_ok_json, ready_timeout=0.05)
    path = tmp_path / "f.bin"
    path.write_bytes(b"data")
    with pytest.raises(TimeoutError):
        await client.upload(chat_id=1, kind="FILE", path=path, mime=None, file_name=None)
    await client.aclose()
    assert waiter._pending == {}


@pytest.mark.asyncio
async def test_upload_throttle_propagates(tmp_path):
    ws = FakeWs(errors={OP_PHOTO_UPLOAD: MaxThrottleError({"code": "too.many"})})
    client = _client(tmp_path, ws, handler=_ok_json)
    path = tmp_path / "p.jpg"
    path.write_bytes(b"x")
    with pytest.raises(MaxThrottleError) as exc_info:
        await client.upload(chat_id=1, kind="PHOTO", path=path, mime=None, file_name=None)
    await client.aclose()
    assert exc_info.value.retry_after_seconds == 30


@pytest.mark.asyncio
async def test_upload_photo_no_url_raises(tmp_path):
    ws = FakeWs(scripted={OP_PHOTO_UPLOAD: {}})
    client = _client(tmp_path, ws, handler=_ok_json)
    path = tmp_path / "p.jpg"
    path.write_bytes(b"x")
    with pytest.raises(MaxMediaError):
        await client.upload(chat_id=1, kind="PHOTO", path=path, mime=None, file_name=None)
    await client.aclose()


@pytest.mark.asyncio
async def test_upload_http_500_raises(tmp_path):
    ws = FakeWs(scripted={OP_PHOTO_UPLOAD: {"url": "https://iu/up"}})

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client = _client(tmp_path, ws, handler=handler)
    path = tmp_path / "p.jpg"
    path.write_bytes(b"x")
    with pytest.raises(MaxMediaError):
        await client.upload(chat_id=1, kind="PHOTO", path=path, mime=None, file_name=None)
    await client.aclose()


# --- Ошибочные ветки upload-слотов (дрейф схемы — куда «ляжет» смоук) --- #


@pytest.mark.asyncio
async def test_upload_slot_without_info_raises_and_cleans_waiter(tmp_path):
    ws = FakeWs(scripted={OP_FILE_UPLOAD: {}})
    waiter = UploadWaiter()
    client = _client(tmp_path, ws, waiter=waiter, handler=_ok_json)
    path = tmp_path / "f.bin"
    path.write_bytes(b"data")
    with pytest.raises(MaxMediaError):
        await client.upload(chat_id=1, kind="FILE", path=path, mime=None, file_name=None)
    await client.aclose()
    assert waiter._pending == {}


@pytest.mark.asyncio
async def test_upload_slot_without_url_raises(tmp_path):
    ws = FakeWs(scripted={OP_FILE_UPLOAD: {"info": [{"fileId": 5}]}})  # нет url
    waiter = UploadWaiter()
    client = _client(tmp_path, ws, waiter=waiter, handler=_ok_json)
    path = tmp_path / "f.bin"
    path.write_bytes(b"data")
    with pytest.raises(MaxMediaError):
        await client.upload(chat_id=1, kind="FILE", path=path, mime=None, file_name=None)
    await client.aclose()
    assert waiter._pending == {}  # регистрация не дошла до POST


@pytest.mark.asyncio
async def test_download_op88_without_url_returns_none(tmp_path):
    ws = FakeWs(scripted={OP_FILE_GET: {}})  # сервер не дал подписанный URL
    client = _client(tmp_path, ws, handler=_ok_json)
    payload = await client.download(
        MaxAttach(kind="FILE", file_id=9), chat_id=1, message_id="m"
    )
    await client.aclose()
    assert payload is None


# ---------------------------------------------------------------------- #
# Download
# ---------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_download_photo_direct_cdn_url(tmp_path):
    ws = FakeWs()

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).startswith("https://i.oneme.ru/i")
        return httpx.Response(200, content=PHOTO_BYTES, headers={"Content-Type": "image/jpeg"})

    storage = MediaStorage(tmp_path / "media")
    client = _client(tmp_path, ws, handler=handler, storage=storage)
    try:
        payload = await client.download(
            MaxAttach(kind="PHOTO", url="https://i.oneme.ru/i?r=abc"), chat_id=1, message_id="m1"
        )
    finally:
        await client.aclose()
    assert payload is not None
    assert payload.mime_type == "image/jpeg"
    assert payload.size == len(PHOTO_BYTES)
    assert payload.file_name is None
    assert payload.path.startswith("in/")
    assert (storage.root / payload.path).read_bytes() == PHOTO_BYTES
    # Фото качается напрямую — WS не дёргается.
    assert ws.calls == []


@pytest.mark.asyncio
async def test_download_file_via_op88(tmp_path):
    ws = FakeWs(scripted={OP_FILE_GET: {"url": "https://fd.oneme.ru/d?sig=1"}})

    def handler(request: httpx.Request) -> httpx.Response:
        # octet-stream от CDN не должен затирать mime из имени файла.
        return httpx.Response(200, content=b"PDFDATA", headers={"Content-Type": "application/octet-stream"})

    storage = MediaStorage(tmp_path / "media")
    client = _client(tmp_path, ws, handler=handler, storage=storage)
    try:
        payload = await client.download(
            MaxAttach(kind="FILE", file_id=9, file_name="report.pdf", size=100),
            chat_id=42,
            message_id="m2",
        )
    finally:
        await client.aclose()
    assert payload is not None
    assert payload.mime_type == "application/pdf"
    assert payload.file_name == "report.pdf"
    assert payload.size == len(b"PDFDATA")
    assert payload.path.endswith(".pdf")
    assert (storage.root / payload.path).read_bytes() == b"PDFDATA"
    # op 88 ушёл с правильными ключами.
    assert ws.calls == [(OP_FILE_GET, {"fileId": 9, "chatId": 42, "messageId": "m2"})]


@pytest.mark.asyncio
async def test_download_video_picks_720(tmp_path):
    ws = FakeWs(
        scripted={
            OP_VIDEO_GET: {"MP4_360": "https://x/360", "MP4_720": "https://x/720"}
        }
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"VIDEODATA", headers={"Content-Type": "video/mp4"})

    client = _client(tmp_path, ws, handler=handler)
    try:
        payload = await client.download(
            MaxAttach(kind="VIDEO", video_id=5), chat_id=1, message_id="m3"
        )
    finally:
        await client.aclose()
    assert payload is not None
    assert payload.mime_type == "video/mp4"
    assert payload.path.endswith(".mp4")


@pytest.mark.asyncio
async def test_download_audio_direct_url(tmp_path):
    ws = FakeWs()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"OGGDATA")

    client = _client(tmp_path, ws, handler=handler)
    try:
        payload = await client.download(
            MaxAttach(kind="AUDIO", url="https://vu.okcdn.ru/a?r=1"), chat_id=1, message_id="m4"
        )
    finally:
        await client.aclose()
    assert payload is not None
    assert payload.mime_type == "audio/ogg"


@pytest.mark.asyncio
async def test_download_audio_without_url_falls_back_to_op88(tmp_path):
    ws = FakeWs(scripted={OP_FILE_GET: {"url": "https://fd/d"}})

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"AUDIODATA")

    client = _client(tmp_path, ws, handler=handler)
    try:
        payload = await client.download(
            MaxAttach(kind="AUDIO", file_id=12), chat_id=1, message_id="m5"
        )
    finally:
        await client.aclose()
    assert payload is not None
    assert [op for op, _ in ws.calls] == [OP_FILE_GET]


@pytest.mark.asyncio
async def test_download_declared_oversize_skipped_without_network(tmp_path):
    ws = FakeWs()
    client = _client(tmp_path, ws, handler=_ok_json, limit=100)
    payload = await client.download(
        MaxAttach(kind="FILE", file_id=9, size=10**9), chat_id=1, message_id="m"
    )
    await client.aclose()
    assert payload is None
    assert ws.calls == []


@pytest.mark.asyncio
async def test_download_content_length_oversize_returns_none(tmp_path):
    ws = FakeWs(scripted={OP_FILE_GET: {"url": "https://fd/d"}})

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 200)

    storage = MediaStorage(tmp_path / "media", max_size_bytes=100)
    client = _client(tmp_path, ws, handler=handler, storage=storage)
    try:
        payload = await client.download(
            MaxAttach(kind="FILE", file_id=9), chat_id=1, message_id="m"
        )
    finally:
        await client.aclose()
    assert payload is None
    _assert_incoming_empty(storage)


@pytest.mark.asyncio
async def test_download_actual_oversize_stream_cleans_partial(tmp_path):
    ws = FakeWs(scripted={OP_FILE_GET: {"url": "https://fd/d"}})

    async def chunks():
        yield b"a" * 60
        yield b"b" * 60  # вторым чанком перевалим лимит 100

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=chunks())

    storage = MediaStorage(tmp_path / "media", max_size_bytes=100)
    client = _client(tmp_path, ws, handler=handler, storage=storage)
    try:
        payload = await client.download(
            MaxAttach(kind="FILE", file_id=9), chat_id=1, message_id="m"
        )
    finally:
        await client.aclose()
    assert payload is None
    _assert_incoming_empty(storage)


@pytest.mark.asyncio
async def test_download_http_404_returns_none(tmp_path):
    ws = FakeWs()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    client = _client(tmp_path, ws, handler=handler)
    payload = await client.download(
        MaxAttach(kind="PHOTO", url="https://i.oneme.ru/i?r=x"), chat_id=1, message_id="m"
    )
    await client.aclose()
    assert payload is None


@pytest.mark.asyncio
async def test_download_without_any_url_returns_none(tmp_path):
    ws = FakeWs()
    client = _client(tmp_path, ws, handler=_ok_json)
    assert (
        await client.download(MaxAttach(kind="PHOTO"), chat_id=1, message_id="m") is None
    )
    assert (
        await client.download(MaxAttach(kind="FILE"), chat_id=1, message_id="m") is None
    )
    await client.aclose()
