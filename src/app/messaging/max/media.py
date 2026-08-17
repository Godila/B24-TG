"""Медиа-механика MAX: upload (WS-опкод → HTTP POST → push 136) и download.

Единственный модуль канала, говорящий по HTTP (httpx): upload-хосты
fu/iu.oneme.ru и vu.okcdn.ru, скачивание входящих — прямой GET по CDN или
подписанной ссылке. WS-запросы уходят через колбэк ``ws_request``
провайдера (обёртка над ТЕКУЩИМ клиентом — переживает реконнекты).

Реверс-протокол дрейфует: ВСЕ чтения полей чужих ответов и push'ей —
толерантные геттеры (upload_slot/photo_upload_token/find_upload_keys/
extract_attach); правки по живому смоуку ложатся сюда и в push_parser,
не задевая оркестрацию провайдера.

Ретраев нет сознательно: upload-URL одноразовый (годен для одного файла),
полный перезапуск пайплайна делает outbox-backoff на новой попытке.
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx

from app.media.storage import MediaStorage, ext_for, normalize_mime
from app.messaging.max.protocol import (
    OP_FILE_GET,
    OP_FILE_UPLOAD,
    OP_PHOTO_UPLOAD,
    OP_UPLOAD_NOTIFY,
    OP_VIDEO_GET,
    OP_VIDEO_UPLOAD,
    MaxAuthError,
    MaxError,
    MaxThrottleError,
    download_url,
    file_attach,
    file_get_payload,
    file_upload_payload,
    photo_attach,
    photo_upload_payload,
    photo_upload_token,
    photo_upload_url,
    to_int,
    upload_notify_payload,
    upload_slot,
    video_attach,
    video_get_payload,
    video_upload_payload,
)
from app.messaging.max.push_parser import MaxAttach
from app.messaging.types import MediaPayload

logger = logging.getLogger(__name__)

#: WS-запрос провайдера: (opcode, payload, *, timeout) → фрейм-ответ.
WsRequest = Callable[..., Awaitable[dict]]

#: Порядок предпочтения качеств из ответа OP_VIDEO_GET. EXTERNAL и
#: dynamicUrl — внешние ссылки-указатели, не наш файл: не качаем.
_VIDEO_QUALITY_ORDER = ("MP4_720", "MP4_480", "MP4_360", "MP4_240")


class MaxMediaError(MaxError):
    """Сбой медиа-пайплайна (нет URL в ответе, HTTP-отказ, превышен лимит)."""


class UploadWaiter:
    """Реестр фьючерсов «запрос-через-push» для OP_UPLOAD_READY(136).

    Готовность file/video-upload приходит push'ем, а не ACK по seq —
    матчим по (вид, id) внутри payload. ``feed`` синхронен (set_result
    планирует пробуждение ожидающего через event loop), поэтому вызывается
    из reader-колбэка провайдера без await — дедлок-инвариант «reader не
    await'ит запросы» не нарушается.
    """

    def __init__(self) -> None:
        self._pending: dict[tuple[str, int], asyncio.Future] = {}

    def expect(self, kind: str, item_id: int) -> asyncio.Future:
        """Зарегистрировать ожидание (ДО HTTP POST: 136 может опередить)."""
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[(kind, item_id)] = fut
        return fut

    def feed(self, payload: object) -> None:
        """Разрешить фьючерсы, чей (вид, id) найден в payload 136."""
        for key in find_upload_keys(payload) & self._pending.keys():
            fut = self._pending.pop(key)
            if not fut.done():
                fut.set_result(True)

    def abandon(self, kind: str, item_id: int) -> None:
        """Снять ожидание (таймаут/успех) — реестр не течёт."""
        self._pending.pop((kind, item_id), None)

    def fail_all(self, exc: Exception) -> None:
        """Разрыв WS/остановка провайдера: ожидающие получают ошибку сразу."""
        pending = list(self._pending.values())
        self._pending.clear()
        for fut in pending:
            if not fut.done():
                fut.set_exception(exc)


def find_upload_keys(payload: object) -> set[tuple[str, int]]:
    """(вид, id) из payload 136: ключи fileId/videoId на любой глубине.

    Толерантно к дрейфу схемы: рекурсивный обход dict/list. Мусорный
    payload без знакомых ключей просто никого не разбудит.
    """
    found: set[tuple[str, int]] = set()
    stack: list[object] = [payload]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "fileId" or key == "videoId":
                    item_id = to_int(value)
                    if item_id is not None:
                        found.add(("file" if key == "fileId" else "video", item_id))
                else:
                    stack.append(value)
        elif isinstance(node, list):
            stack.extend(node)
    return found


def _url_of(value: object) -> str | None:
    """URL из значения качества: строка или {url: …}."""
    if isinstance(value, str) and value.startswith(("http://", "https://")):
        return value
    if isinstance(value, dict):
        url = value.get("url")
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            return url
    return None


def pick_video_url(payload: dict) -> str | None:
    """Выбор качества из ответа OP_VIDEO_GET: 720→480→360→240→любой MP4_*."""
    for quality in _VIDEO_QUALITY_ORDER:
        url = _url_of(payload.get(quality))
        if url:
            return url
    for key, value in payload.items():
        if key.startswith("MP4"):
            url = _url_of(value)
            if url:
                return url
    return None


def fallback_mime(attach: MaxAttach) -> str | None:
    """Mime бедного вложения (в кадре не было mimeType)."""
    if attach.kind == "PHOTO":
        return "image/jpeg"
    if attach.kind == "VIDEO":
        return "video/mp4"
    if attach.kind == "AUDIO":
        return "audio/ogg"
    if attach.file_name:
        return mimetypes.guess_type(attach.file_name)[0]
    return None


class MaxMediaClient:
    """HTTP-механика медиа MAX поверх WS-сессии провайдера.

    Один ``httpx.AsyncClient`` на провайдера, создаётся лениво и НЕ
    закрывается при WS-реконнектах (HTTP-пул не связан с WS-сессией);
    ``aclose`` вызывается из disconnect(). Тестовый шов — ``http_factory``
    (httpx.MockTransport).
    """

    def __init__(
        self,
        *,
        storage: MediaStorage,
        ws_request: WsRequest,
        waiter: UploadWaiter,
        headers: dict[str, str],
        upload_ready_timeout_sec: float = 60.0,
        http_factory: Callable[[], httpx.AsyncClient] | None = None,
    ):
        self._storage = storage
        self._ws_request = ws_request
        self._waiter = waiter
        self._headers = dict(headers)
        self._upload_ready_timeout = upload_ready_timeout_sec
        self._http_factory = http_factory
        self._http: httpx.AsyncClient | None = None

    # ------------------------------------------------------------------ #
    # HTTP-жизнь
    # ------------------------------------------------------------------ #
    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            if self._http_factory is not None:
                self._http = self._http_factory()
            else:
                self._http = httpx.AsyncClient(
                    timeout=httpx.Timeout(10.0, read=120.0, write=120.0, pool=10.0),
                    headers=self._headers,
                    follow_redirects=True,
                )
        return self._http

    async def aclose(self) -> None:
        if self._http is not None:
            try:
                await self._http.aclose()
            except Exception:  # best-effort: пул мог уже умереть
                logger.debug("MAX media http close best-effort", exc_info=True)
            self._http = None

    # ------------------------------------------------------------------ #
    # Исходящие: файл на томе → элементы attaches[] для MSG_SEND
    # ------------------------------------------------------------------ #
    async def upload(
        self,
        *,
        chat_id: int,
        kind: str,
        path: Path,
        mime: str | None,
        file_name: str | None,
    ) -> list[dict]:
        """Загрузить файл и собрать attaches (PHOTO/VIDEO/FILE).

        FILE-путь принимают и аудио: нативная voice-загрузка (op82/type=2)
        требует OGG Opus и исходящих голосовых у нас нет.
        Выбрасывает MaxError/MaxThrottleError/TimeoutError — маппинг в
        SendResult делает провайдер.
        """
        if kind == "PHOTO":
            return [await self._upload_photo(path=path, mime=mime, file_name=file_name)]
        if kind == "VIDEO":
            return [
                await self._upload_video(
                    chat_id=chat_id, path=path, mime=mime, file_name=file_name
                )
            ]
        return [
            await self._upload_file(chat_id=chat_id, path=path, mime=mime, file_name=file_name)
        ]

    async def _notify_upload(self, chat_id: int, kind: str) -> None:
        """op 65 — уведомление о начале загрузки (шлёт vkmax; сбой ≠ abort).

        Auth/throttle обязаны выйти наружу — они касаются всей сессии.
        """
        try:
            await self._ws_request(OP_UPLOAD_NOTIFY, upload_notify_payload(chat_id, kind))
        except (MaxAuthError, MaxThrottleError):
            raise
        except Exception as exc:  # noqa: BLE001 - уведомление best-effort
            logger.warning("MAX op65 upload-notify не прошёл (%s) — продолжаем", exc)

    async def _upload_photo(self, *, path: Path, mime: str | None, file_name: str | None) -> dict:
        resp = await self._ws_request(OP_PHOTO_UPLOAD, photo_upload_payload())
        url = photo_upload_url(resp.get("payload") or {})
        if not url:
            raise MaxMediaError(f"OP_PHOTO_UPLOAD без url: {str(resp.get('payload'))[:200]}")
        body = await self._post_multipart(url, path=path, mime=mime, file_name=file_name)
        token = photo_upload_token(body)
        if not token:
            raise MaxMediaError(f"photo upload без token: {str(body)[:200]}")
        return photo_attach(token)

    async def _upload_file(self, *, chat_id: int, path: Path, mime: str | None, file_name: str | None) -> dict:
        await self._notify_upload(chat_id, "FILE")
        resp = await self._ws_request(OP_FILE_UPLOAD, file_upload_payload())
        slot = upload_slot(resp.get("payload") or {})
        if slot is None:
            raise MaxMediaError(f"OP_FILE_UPLOAD без info[0]: {str(resp.get('payload'))[:200]}")
        url, file_id, _token = slot
        if not url or file_id is None:
            raise MaxMediaError(f"OP_FILE_UPLOAD без url/fileId: {str(resp.get('payload'))[:200]}")
        # Регистрация ДО POST: 136 может прийти, пока POST ещё идёт.
        fut = self._waiter.expect("file", file_id)
        try:
            await self._post_multipart(url, path=path, mime=mime, file_name=file_name)
            await asyncio.wait_for(fut, self._upload_ready_timeout)
        finally:
            self._waiter.abandon("file", file_id)
        return file_attach(file_id)

    async def _upload_video(self, *, chat_id: int, path: Path, mime: str | None, file_name: str | None) -> dict:
        await self._notify_upload(chat_id, "VIDEO")
        resp = await self._ws_request(OP_VIDEO_UPLOAD, video_upload_payload())
        slot = upload_slot(resp.get("payload") or {})
        if slot is None:
            raise MaxMediaError(f"OP_VIDEO_UPLOAD без info[0]: {str(resp.get('payload'))[:200]}")
        url, video_id, token = slot
        if not url or video_id is None:
            raise MaxMediaError(f"OP_VIDEO_UPLOAD без url/videoId: {str(resp.get('payload'))[:200]}")
        fut = self._waiter.expect("video", video_id)
        try:
            await self._post_multipart(url, path=path, mime=mime, file_name=file_name)
            await asyncio.wait_for(fut, self._upload_ready_timeout)
        finally:
            self._waiter.abandon("video", video_id)
        return video_attach(video_id, token)

    async def _post_multipart(
        self, url: str, *, path: Path, mime: str | None, file_name: str | None
    ) -> dict:
        """POST файла на upload-URL (multipart, поле ``file``) → JSON ответа.

        Файл ≤25 МБ на локальном томе — читаем в память (паритет с
        философией MediaStorage: синхронный I/O тут дешевле сложности).
        """
        data = path.read_bytes()
        resp = await self._client().post(
            url,
            files={"file": (file_name or path.name, data, mime or "application/octet-stream")},
        )
        if resp.status_code >= 400:
            raise MaxMediaError(f"upload HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            return resp.json()
        except ValueError as exc:
            raise MaxMediaError(f"upload ответ не JSON: {resp.text[:200]}") from exc

    # ------------------------------------------------------------------ #
    # Входящие: вложение push'а → файл на томе (или None = плейсхолдер)
    # ------------------------------------------------------------------ #
    async def download(
        self,
        attach: MaxAttach,
        *,
        chat_id: int,
        message_id: str | int | None,
    ) -> MediaPayload | None:
        """Скачать вложение; None = сбой (остаётся текст-плейсхолдер).

        Инварианты — паритет с TelegramProvider._download_media: declared
        размер проверяется до сети, фактический после; путь пишет
        MediaStorage (uuid-имя); любая ошибка → None, сообщение не теряется.
        """
        if attach.size is not None and (
            self._storage.max_size_bytes is not None
            and attach.size > self._storage.max_size_bytes
        ):
            logger.info(
                "MAX вложение больше лимита (size=%s, kind=%s) — плейсхолдер",
                attach.size,
                attach.kind,
            )
            return None
        try:
            url = await self._resolve_download_url(
                attach, chat_id=chat_id, message_id=message_id
            )
            if not url:
                logger.warning(
                    "MAX вложение без URL (kind=%s, keys=%s) — плейсхолдер",
                    attach.kind,
                    sorted(attach.raw.keys()),
                )
                return None
            mime = attach.mime or fallback_mime(attach)
            absolute, relative = self._storage.new_path(
                direction="in", ext=ext_for(attach.file_name, mime)
            )
            try:
                size, content_type = await self._download_to(url, absolute)
                # Заголовок ответа богаче догадки, но только если настоящий
                # (не octet-stream заглушка CDN).
                if content_type:
                    header_mime = normalize_mime(content_type)
                    if header_mime and header_mime != "application/octet-stream":
                        mime = header_mime
                return MediaPayload(
                    path=relative,
                    mime_type=normalize_mime(mime),
                    size=size,
                    file_name=attach.file_name,
                )
            except Exception:
                absolute.unlink(missing_ok=True)  # частичный файл — мусор
                raise
        except Exception:
            logger.warning(
                "MAX download вложения не удался (kind=%s)", attach.kind, exc_info=True
            )
            return None

    async def _resolve_download_url(
        self, attach: MaxAttach, *, chat_id: int, message_id: str | int | None
    ) -> str | None:
        kind = attach.kind
        if kind == "PHOTO":
            # Фото: готовый CDN-URL прямо во вложении (без авторизации).
            return attach.url
        if kind == "AUDIO":
            # По комьюнити-моделям url лежит в самом вложении; если нет —
            # пробуем подписанный файловый путь (не подтверждено живьём).
            return attach.url or await self._signed_url(
                attach, chat_id=chat_id, message_id=message_id
            )
        if kind == "FILE":
            return await self._signed_url(attach, chat_id=chat_id, message_id=message_id)
        if kind == "VIDEO":
            if attach.video_id is None:
                return None
            resp = await self._ws_request(
                OP_VIDEO_GET, video_get_payload(attach.video_id, chat_id, message_id)
            )
            return pick_video_url(resp.get("payload") or {})
        return None

    async def _signed_url(
        self, attach: MaxAttach, *, chat_id: int, message_id: str | int | None
    ) -> str | None:
        if attach.file_id is None:
            return None
        resp = await self._ws_request(
            OP_FILE_GET, file_get_payload(attach.file_id, chat_id, message_id)
        )
        return download_url(resp.get("payload") or {})

    async def _download_to(self, url: str, dest: Path) -> tuple[int, str | None]:
        """Стриминг GET → файл; (фактический размер, Content-Type).

        Content-Length (declared) проверяется до записи, переполнение в
        потоке — по ходу; превышение капа → MaxMediaError (частичный файл
        удалит вызывающий).
        """
        limit = self._storage.max_size_bytes
        written = 0
        async with self._client().stream("GET", url) as resp:
            if resp.status_code >= 400:
                raise MaxMediaError(f"download HTTP {resp.status_code}")
            declared = resp.headers.get("Content-Length")
            if limit is not None and declared and declared.isdigit() and int(declared) > limit:
                raise MaxMediaError(f"Content-Length {declared} > {limit}")
            content_type = resp.headers.get("Content-Type")
            with dest.open("wb") as fh:
                async for chunk in resp.aiter_bytes(64 * 1024):
                    written += len(chunk)
                    if limit is not None and written > limit:
                        raise MaxMediaError(f"получено {written} байт > лимита {limit}")
                    fh.write(chunk)
        return written, content_type
