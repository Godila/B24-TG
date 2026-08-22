"""WhatsAppProvider — реализация MessengerProvider поверх OpenWA-сайдкара.

Паттерн MaxUserProvider: REST (OpenWaClient) — команды, Socket.IO
(WaEventClient) — живые события в очереди; тяжёлое обогащение (скачивание
медиа) — в push-воркере, НЕ в колбэке сокета; эхо-сет недавних отправок
отделяет echo наших REST-сends от device-outbound (менеджер написал с
телефона); supervise-поллинг сурфейсит restriction/failed. Сессия живёт в
OpenWA и переживает рестарт bridge — здесь только её id
(credential_token = wa_session_id, перепривязка ловится AccountSync).
"""

import asyncio
import base64
import logging
import re
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

from app.messaging.provider import MessengerProvider, SessionRevokedError
from app.messaging.resolve import ParsedDest, ResolvedPeer
from app.messaging.types import (
    MEDIA_PLACEHOLDERS,
    ContentType,
    IncomingMessage,
    MediaPayload,
    ReadReceipt,
    SendResult,
)
from app.messaging.whatsapp.api import OpenWaClient, WaAuthError, WaError
from app.messaging.whatsapp.events import WaEventClient
from app.messaging.whatsapp.media import WaMedia
from app.models import MessageDirection, Messenger

logger = logging.getLogger(__name__)

_STREAM_END = None  # сентинел завершения потоков (паттерн MAX)

_ECHO_MAX_IDS = 512
#: Грейс на пуш message.sent, обогнавший REST-ответ send-*: continuation
#: регистрации id выполняется раньше (паттерн MAX _ECHO_GRACE_SEC).
_ECHO_GRACE_SEC = 0.5
_STATUS_POLL_SEC = 15.0
_REGISTER_TIMEOUT_SEC = 30.0
_READY_POLL_SEC = 2.0

#: Личный чат WA: <digits>@c.us (телефон) или <digits>@lid (privacy-id).
_CHAT_ID_RE = re.compile(r"^\d+@(c\.us|lid)$")

#: type события OpenWA → ContentType; отсутствующие (location/contact/poll/
#: call/…) v1 пропускает — плейсхолдеров для них нет, текста нет.
_CONTENT_TYPES: dict[str, ContentType] = {
    "text": ContentType.text,
    "image": ContentType.photo,
    "video": ContentType.video,
    "audio": ContentType.voice,
    "voice": ContentType.voice,
    "document": ContentType.file,
    "sticker": ContentType.sticker,
}

#: ContentType → маршрут send-* OpenWA.
_MEDIA_KINDS: dict[ContentType, str] = {
    ContentType.photo: "image",
    ContentType.video: "video",
    ContentType.voice: "audio",
    ContentType.file: "document",
    ContentType.sticker: "sticker",
}


def _chat_id(raw: str | None) -> str | None:
    """Валидный id личного чата WA или None (группа @g.us/мусор)."""
    return raw if _CHAT_ID_RE.match(raw or "") else None


def _id_digits(raw: str | None) -> str | None:
    """«62812…@c.us» → «62812…» (телефон или lid-число)."""
    if not raw:
        return None
    return raw.split("@", 1)[0] or None


def _name_parts(name: str | None) -> tuple[str | None, str | None]:
    if not name:
        return None, None
    parts = name.split(" ", 1)
    return parts[0], parts[1] if len(parts) > 1 else None


class WhatsAppProvider(MessengerProvider):
    """Один аккаунт (линия) = одна сессия OpenWA = один Socket.IO-коннект.

    Для тестов: ``api``/``media``/``events_factory`` подменяются фейками
    до connect() — реальной сети в юнит-тестах нет.
    """

    def __init__(
        self,
        *,
        session_id: str,
        api: OpenWaClient,
        media: WaMedia | None = None,
        base_url: str = "",
        api_key: str = "",
        events_factory=None,
    ) -> None:
        self._session_id = session_id
        self._api = api
        self._media = media
        self._status_poll_sec = _STATUS_POLL_SEC
        self._register_timeout = _REGISTER_TIMEOUT_SEC
        self._ready_poll_sec = _READY_POLL_SEC
        self._echo_grace_sec = _ECHO_GRACE_SEC
        self._incoming_queue: asyncio.Queue[IncomingMessage | None] = asyncio.Queue()
        self._read_queue: asyncio.Queue[ReadReceipt | None] = asyncio.Queue()
        # Тяжёлое обогащение — НЕ в колбэке сокета (урок MAX-дедлока):
        # колбэк кладёт сырой пуш сюда, воркер скачивает медиа последовательно.
        self._push_queue: asyncio.Queue[tuple[str, dict]] = asyncio.Queue()
        self._push_worker: asyncio.Task | None = None
        self._supervisor: asyncio.Task | None = None
        self._stopped = False
        self._dead = False
        self._restriction: dict | None = None
        # Эхо-сет + маршрут квитанций: message id → chat id (bounded).
        self._known_sends: dict[str, str] = {}
        events_factory = events_factory or (
            lambda: WaEventClient(
                base_url=base_url, api_key=api_key, on_event=self._on_event
            )
        )
        self._events: WaEventClient = events_factory()

    # ------------------------------------------------------------------ #
    # Контракт MessengerProvider
    # ------------------------------------------------------------------ #

    async def connect(self) -> None:
        """Сессия готова + подписка на события. Гибель сессии (failed/
        tos_block) → SessionRevokedError (терминально для AccountSync)."""
        try:
            await self._ensure_ready()
            await self._events.start(self._session_id)
        except WaAuthError as exc:
            self._dead = True
            await self._api.aclose()  # MAX-паттерн: провал не оставляет пул
            raise SessionRevokedError(str(exc)) from exc
        except Exception:
            await self._api.aclose()
            raise
        # restriction уже загружен _ensure_ready — НЕ стираем (timelock
        # сосуществует с ready по спеке OpenWA, бейдж не должен мигать).
        self._push_worker = asyncio.create_task(self._push_worker_loop())
        self._supervisor = asyncio.create_task(self._supervise_loop())
        logger.info("WA online: session=%s", self._session_id[:8])

    async def disconnect(self) -> None:
        self._stopped = True
        for task in (self._supervisor, self._push_worker):
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception:  # защитная сетка отмены
                    logger.debug("wa task cancel", exc_info=True)
        self._supervisor = None
        self._push_worker = None
        await self._events.stop()
        await self._api.aclose()
        await self._incoming_queue.put(_STREAM_END)
        await self._read_queue.put(_STREAM_END)

    def is_connected(self) -> bool:
        return not self._dead and self._events.is_connected()

    def is_dead(self) -> bool:
        return self._dead

    def credential_token(self) -> str | None:
        return self._session_id

    def restriction(self) -> dict | None:
        """Текущее ограничение WhatsApp (AccountSync пишет в строку линии)."""
        return self._restriction

    async def incoming_stream(self) -> AsyncIterator[IncomingMessage]:
        while True:
            msg = await self._incoming_queue.get()
            if msg is None:
                break
            yield msg

    async def read_receipt_stream(self) -> AsyncIterator[ReadReceipt]:
        while True:
            receipt = await self._read_queue.get()
            if receipt is None:
                break
            yield receipt

    async def send_message(
        self, external_chat_id: str, text: str, *, is_initiation: bool
    ) -> SendResult:
        if self._dead or not self._events.is_connected():
            return SendResult(success=False, error="not connected")
        if not _CHAT_ID_RE.match(external_chat_id):
            return SendResult(success=False, error=f"bad_chat_id: {external_chat_id!r}")
        try:
            resp = await self._api.send_text(self._session_id, external_chat_id, text)
        except WaAuthError as exc:
            self._dead = True
            logger.error("WA api-ключ отвергнут (send): %s", exc)
            return SendResult(success=False, error="wa_auth")
        except WaError as exc:
            return self._wa_error_result(exc)
        # 201 = принято, НЕ доставлено; доставка — асинхронные ack (Э6).
        message_id = str(resp.get("messageId")) if resp.get("messageId") else None
        if message_id:
            self._remember_send(message_id, external_chat_id)
        return SendResult(success=True, external_message_id=message_id)

    def supports_media(self) -> bool:
        return self._media is not None

    async def send_media(
        self,
        external_chat_id: str,
        path: Path,
        content_type: ContentType,
        *,
        mime_type: str | None = None,
        file_name: str | None = None,
        caption: str | None = None,
        is_initiation: bool = False,
    ) -> SendResult:
        """Base64 из файла общего тома (url-вариант блокирует SSRF-guard).
        Вселенная ретраев — outbox: каждая попытка кодирует файл заново."""
        if self._dead or not self._events.is_connected() or self._media is None:
            return SendResult(success=False, error="not connected")
        if not _CHAT_ID_RE.match(external_chat_id):
            return SendResult(success=False, error=f"bad_chat_id: {external_chat_id!r}")
        kind = _MEDIA_KINDS.get(content_type, "document")
        try:
            resp = await self._api.send_media(
                self._session_id,
                external_chat_id,
                kind=kind,
                b64=base64.b64encode(path.read_bytes()).decode("ascii"),
                mimetype=mime_type or "application/octet-stream",
                filename=file_name,
                caption=caption,
            )
        except WaAuthError as exc:
            self._dead = True
            logger.error("WA api-ключ отвергнут (send_media): %s", exc)
            return SendResult(success=False, error="wa_auth")
        except WaError as exc:
            return self._wa_error_result(exc)
        except OSError as exc:
            return SendResult(success=False, error=f"file_read: {exc}")
        message_id = str(resp.get("messageId")) if resp.get("messageId") else None
        if message_id:
            self._remember_send(message_id, external_chat_id)
        return SendResult(success=True, external_message_id=message_id)

    async def resolve_peer(self, dest: ParsedDest) -> ResolvedPeer | None:
        """Телефон → contacts/check. None = номера нет на WA (терминально,
        как not.found у MAX). Приватный чат id = whatsappId (= phone@c.us)."""
        if dest.kind != "phone":
            return None
        if self._dead:
            raise ConnectionError("wa provider not connected")
        digits = dest.value.lstrip("+")
        try:
            info = await self._api.check_contact(self._session_id, digits)
        except WaAuthError as exc:
            raise SessionRevokedError(str(exc)) from exc
        if not info.get("exists"):
            return None
        chat_id = info.get("whatsappId") or f"{digits}@c.us"
        return ResolvedPeer(external_user_id=digits, external_chat_id=chat_id, phone=dest.value)

    # ------------------------------------------------------------------ #
    # События Socket.IO (sync-колбэки — только лёгкие очереди)
    # ------------------------------------------------------------------ #

    def _on_event(self, payload: dict) -> None:
        event = payload.get("event")
        data = payload.get("data") or {}
        if event == "message.received":
            self._push_queue.put_nowait(("received", data))
        elif event == "message.sent":
            self._push_queue.put_nowait(("sent", data))
        elif event == "message.ack":
            self._handle_ack(data)
        elif event == "session.status":
            self._handle_status(data)
        elif event == "session.disconnected":
            logger.info(
                "WA session %s disconnected: %s", self._session_id, data.get("reason")
            )
        elif event == "session.restriction":
            self._handle_restriction(data)

    def _handle_ack(self, data: dict) -> None:
        if data.get("status") != "read":
            return  # delivered в контракт провайдера не входит
        message_id = str(data.get("messageId") or "")
        chat_id = self._known_sends.get(message_id)
        if not message_id or chat_id is None:
            return  # id вне сети (рестарт/переполнение) — маршрут неизвестен
        self._read_queue.put_nowait(
            ReadReceipt(
                messenger=Messenger.wa,
                external_chat_id=chat_id,
                up_to_external_id=None,
                external_message_id=message_id,
            )
        )

    def _handle_status(self, data: dict) -> None:
        if data.get("status") == "failed":
            self._dead = True
            logger.error("WA session %s failed: %s", self._session_id, data)

    def _handle_restriction(self, data: dict) -> None:
        active = bool(data.get("active"))
        self._restriction = data if active else None
        if active and data.get("kind") == "tos_block":
            self._dead = True
        logger.warning("WA restriction: %s", data)

    # ------------------------------------------------------------------ #
    # Внутреннее
    # ------------------------------------------------------------------ #

    async def _ensure_ready(self) -> None:
        info = await self._api.get_session(self._session_id)
        self._restriction = info.get("restriction")
        if _is_tos_block(self._restriction):
            raise SessionRevokedError("wa tos_block: номер заблокирован WhatsApp")
        if info.get("status") == "ready":
            return
        if info.get("status") == "failed":
            raise SessionRevokedError(
                f"wa session failed: {info.get('lastError') or 'unknown'}"
            )
        if not info.get("engineLoaded"):
            await self._api.start_session(self._session_id)
        deadline = time.monotonic() + self._register_timeout
        status = info.get("status")
        while time.monotonic() < deadline:
            await asyncio.sleep(self._ready_poll_sec)
            info = await self._api.get_session(self._session_id)
            status = info.get("status")
            self._restriction = info.get("restriction")
            if _is_tos_block(self._restriction):
                raise SessionRevokedError("wa tos_block: номер заблокирован WhatsApp")
            if status == "ready":
                return
            if status == "failed":
                raise SessionRevokedError(
                    f"wa session failed: {info.get('lastError') or 'unknown'}"
                )
        raise TimeoutError(f"wa session not ready in {self._register_timeout}s: {status}")

    async def _push_worker_loop(self) -> None:
        while True:
            kind, data = await self._push_queue.get()
            try:
                if kind == "received":
                    msg = await self._parse_message(data, inbound=True)
                    if msg is not None:
                        self._incoming_queue.put_nowait(msg)
                else:  # sent: эхо-фильтр + device-outbound
                    await self._handle_sent(data)
            except Exception:
                logger.exception("WA push handler failed: %s", kind)

    async def _parse_message(self, data: dict, *, inbound: bool) -> IncomingMessage | None:
        """Событие message.* → IncomingMessage; None = пропустить (группа,
        неподдерживаемый тип, не-личный чат)."""
        if (data.get("kind") or "individual") != "individual":
            return None
        ctype = _CONTENT_TYPES.get(data.get("type") or "text")
        if ctype is None:
            logger.debug("WA unsupported message type: %s", data.get("type"))
            return None
        # received: from = клиент; device-sent: from = владелец, to = клиент.
        wa_id = data.get("from") if inbound else data.get("to")
        chat_id = _chat_id(wa_id)
        if chat_id is None:
            return None
        contact = data.get("contact") or {}
        name = contact.get("name") or contact.get("pushName")
        # @lid-отправитель: RESOLVE_LID_TO_PHONE даёт senderPhone — он
        # стабильнее lid-числа для CRM-матчинга, поэтому внешний id = телефон.
        sender_phone = data.get("senderPhone") or (
            _id_digits(wa_id) if str(wa_id).endswith("@c.us") else None
        )
        first, last = _name_parts(name)
        media: MediaPayload | None = None
        # Триггер: hasMedia ИЛИ media-мета (Baileys иногда шлёт без hasMedia —
        # ловилось пустым пузырём без вложения, грабля 08-22).
        has_media = bool(data.get("hasMedia") or data.get("media"))
        if ctype is not ContentType.text and has_media and self._media:
            meta = data.get("media") or {}
            try:
                media = await self._media.download(
                    session_id=self._session_id,
                    chat_id=chat_id,
                    message_id=str(data.get("id") or ""),
                    mimetype=meta.get("mimetype"),
                    file_name=meta.get("filename"),
                    direction="in" if inbound else "out",
                )
            except Exception:
                logger.warning("WA media download failed msg=%s", data.get("id"), exc_info=True)
        # Плейсхолдер вместо пустого пузыря: медиа не скачалось/нет — текст
        # несёт «[фото]»/«[голосовое сообщение]» (конвенция TG; таймлайн B24
        # и превью его видят, UI-пузырь скрывает при вложении).
        body = data.get("body") or None
        if media is None and ctype is not ContentType.text:
            body = body or MEDIA_PLACEHOLDERS[ctype]
        return IncomingMessage(
            messenger=Messenger.wa,
            external_chat_id=chat_id,
            sender_external_id=sender_phone or _id_digits(wa_id) or "",
            sender_name=name,
            sender_phone=sender_phone,
            sender_username=None,
            content_type=ctype,
            text=body,
            media=media,
            external_message_id=str(data.get("id")) if data.get("id") else None,
            timestamp=(
                datetime.fromtimestamp(data["timestamp"], tz=UTC)
                if data.get("timestamp")
                else None
            ),
            is_reply=bool(data.get("quotedMessage")),
            sender_first_name=first,
            sender_last_name=last,
            direction=(
                MessageDirection.inbound if inbound else MessageDirection.outbound
            ),
        )

    async def _handle_sent(self, data: dict) -> None:
        """message.sent: эхо нашего REST-отправления (id в сети) — мимо;
        остальное — менеджер написал с телефона (device-outbound)."""
        message_id = str(data.get("id") or "")
        chat_id = _chat_id(data.get("to"))
        if not message_id or chat_id is None:
            return
        if message_id in self._known_sends:
            return
        # Пуш мог обогнать REST-ответ send-*: continuation регистрации id
        # ещё не выполнился. Короткий грейс и перепроверка (паттерн MAX);
        # после грейса id неизвестен → честный device-outbound.
        await asyncio.sleep(self._echo_grace_sec)
        if message_id in self._known_sends:
            return
        self._remember_send(message_id, chat_id)
        msg = await self._parse_message(data, inbound=False)
        if msg is not None:
            self._incoming_queue.put_nowait(msg)

    def _remember_send(self, message_id: str, chat_id: str) -> None:
        """Запомнить id → chat (эхо-сет и маршрут квитанций, bounded).

        In-memory по построению: рестарт bridge теряет сет, но пережившие
        рестарт отправки уже закоммитили external_message_id в Message —
        эхо ловит БД-дедуп IncomingHandler.
        """
        self._known_sends.pop(message_id, None)
        self._known_sends[message_id] = chat_id
        while len(self._known_sends) > _ECHO_MAX_IDS:
            del self._known_sends[next(iter(self._known_sends))]

    def _wa_error_result(self, exc: WaError) -> SendResult:
        retry_after = getattr(exc, "retry_after_sec", None)
        if retry_after:
            return SendResult(
                success=False, error="wa_throttle", retry_after_seconds=int(retry_after)
            )
        return SendResult(success=False, error=str(exc))

    async def _supervise_loop(self) -> None:
        """Поллинг статуса сессии: restriction/failed → is_dead/ограничение.
        Socket.IO реконнектится штатно; потерю событий движок перепушит."""
        while not self._stopped:
            try:
                await asyncio.sleep(self._status_poll_sec)
                if self._stopped:
                    break
                info = await self._api.get_session(self._session_id)
                self._restriction = info.get("restriction")
                if info.get("status") == "failed" or _is_tos_block(self._restriction):
                    self._dead = True
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("wa status poll failed", exc_info=True)


def _is_tos_block(restriction: dict | None) -> bool:
    return bool(restriction) and restriction.get("kind") == "tos_block"
