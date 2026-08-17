"""MaxUserProvider — Telethon-подобный провайдер канала MAX.

Одно долгоживущее WS-соединение на аккаунт (менеджера). Критично: ~30-50
LOGIN одним токеном за короткое время сбрасывают токен — поэтому соединение
держится supervise-циклом провайдера (реконнект с backoff 2..32с), а не
пересоздаётся снаружи.

Жизненный цикл:
    connect() → INIT(deviceId) + LOGIN(token) → supervise-таск
      ├─ обрыв → backoff 2,4,8,16,32с → INIT+LOGIN тем же токеном
      ├─ тишина >15с → свой ping (серверный op=1 автоотвечается в ws_client)
      └─ MaxAuthError (токен отозван) → провайдер «мёртв»: is_connected()
         навсегда False, HealthChecker переведёт аккаунт в offline и
         алертнёт админу; менеджер переподключается через /admin/max.
"""

import asyncio
import itertools
import logging
import time
from collections.abc import AsyncIterator
from pathlib import Path

from app.media.storage import MediaStorage
from app.messaging.max.media import MaxMediaClient, UploadWaiter
from app.messaging.max.protocol import (
    OP_CHAT_INFO,
    OP_GET_CONTACTS,
    OP_INIT,
    OP_LOGIN,
    OP_MSG_SEND,
    OP_PING,
    OP_UPLOAD_READY,
    UPLOAD_KIND_FILE,
    UPLOAD_KIND_PHOTO,
    UPLOAD_KIND_VIDEO,
    MaxAuthError,
    init_payload,
    login_payload,
    msg_send_payload,
)
from app.messaging.max.push_parser import (
    contact_display_name,
    contact_name_parts,
    contact_phone,
    parse_message_push,
)
from app.messaging.max.ws_client import MaxWsClient
from app.messaging.provider import MessengerProvider
from app.messaging.types import ContentType, IncomingMessage, MediaPayload, SendResult
from app.models import Messenger

logger = logging.getLogger(__name__)

#: Сигнал конца incoming_stream при disconnect() (forward-таска завершается).
_STREAM_END: IncomingMessage | None = None

#: ContentType → вид загрузки MAX; прочее (voice/file/sticker) идёт FILE-
#: пайплайном: нативная voice-загрузка требует OGG Opus, исходящих
#: голосовых у нас нет — аудио уходит играбельным файлом.
_UPLOAD_KINDS = {
    ContentType.photo: UPLOAD_KIND_PHOTO,
    ContentType.video: UPLOAD_KIND_VIDEO,
    ContentType.file: UPLOAD_KIND_FILE,
}


class MaxUserProvider(MessengerProvider):
    """Реализация MessengerProvider поверх WS-протокола web-клиента MAX.

    Для тестов: подмените ``_client`` фейком до ``connect()`` (или передайте
    ``client_factory``) — реальной сети в юнит-тестах нет.
    """

    def __init__(
        self,
        *,
        token: str,
        device_id: str,
        own_user_id: int | None,
        ws_url: str,
        headers: dict[str, str],
        user_agent: dict,
        request_timeout: float = 15.0,
        heartbeat_idle_sec: float = 15.0,
        heartbeat_tick_sec: float = 5.0,
        backoff_min_sec: float = 2.0,
        backoff_max_sec: float = 32.0,
        client_factory=None,
        media_storage: MediaStorage | None = None,
        media_download_timeout_sec: float = 120.0,
        media_send_timeout_sec: float = 300.0,
        upload_ready_timeout_sec: float = 60.0,
        http_factory=None,
    ):
        self._token = token
        self._device_id = device_id
        self._own_user_id = own_user_id
        self._user_agent = dict(user_agent)
        self._request_timeout = request_timeout
        self._heartbeat_idle_sec = heartbeat_idle_sec
        self._heartbeat_tick_sec = heartbeat_tick_sec
        self._backoff_min_sec = backoff_min_sec
        self._backoff_max_sec = backoff_max_sec
        self._incoming_queue: asyncio.Queue[IncomingMessage | None] = asyncio.Queue()
        self._supervisor: asyncio.Task | None = None
        self._stopped = False
        self._dead = False  # токен отозван — реконнект бессмыслен
        self._cid_counter = itertools.count()
        # Обогащение входящих (имя/тип чата) — НЕ в reader-таске WS: reader
        # ждёт on_push, а on_push с await request() внутри = дедлок (ответ
        # не сможет прийти, пока reader занят колбэком). Reader кладёт
        # распарсенный push в очередь, этот воркер последовательно (порядок
        # сохраняется) делает CHAT_INFO/GET_CONTACTS и кладёт в incoming.
        self._push_queue: asyncio.Queue = asyncio.Queue()
        self._push_worker: asyncio.Task | None = None
        # Кэши обогащения: chatId → диалог ли; userId → (имя, телефон).
        self._chat_is_dialog_cache: dict[str, bool] = {}
        self._sender_cache: dict[int, tuple[str | None, str | None, str | None, str | None]] = {}
        # Один seam на все случаи (первое подключение И реконнекты) —
        # тесты подменяют фабрику целиком, реальной сети в юнит-тестах нет.
        self._client_factory = client_factory or (
            lambda: MaxWsClient(url=ws_url, headers=headers, request_timeout=request_timeout)
        )
        self._client = self._client_factory()
        self._client.on_push(self._on_push)
        # Медиа: None = выключено (тесты/онбординг) — supports_media() False.
        self._media_download_timeout = media_download_timeout_sec
        self._media_send_timeout = media_send_timeout_sec
        self._waiter = UploadWaiter()
        self._media = (
            MaxMediaClient(
                storage=media_storage,
                ws_request=self._ws_request,
                waiter=self._waiter,
                headers=headers,
                upload_ready_timeout_sec=upload_ready_timeout_sec,
                http_factory=http_factory,
            )
            if media_storage is not None
            else None
        )

    # ------------------------------------------------------------------ #
    # Контракт MessengerProvider
    # ------------------------------------------------------------------ #
    async def connect(self) -> None:
        """Первое подключение: INIT+LOGIN.

        MaxAuthError вылетает наружу (аккаунт с мёртвым токеном не
        регистрируется — AccountSyncWorker переведёт его в offline).
        Сетевые сбои при первом подключении тоже наружу: supervise-цикл ещё
        не запущен, bridge должен видеть честный результат регистрации.
        В обоих случаях WS-клиент закрывается — иначе соединение-зомби
        (reader-таска + авто-pong) переживёт неудачную регистрацию.
        """
        try:
            await self._connect_once()
        except MaxAuthError:
            self._dead = True
            await self._safe_close_client()
            raise
        except Exception:
            await self._safe_close_client()
            raise
        self._supervisor = asyncio.create_task(self._supervise_loop())
        self._push_worker = asyncio.create_task(self._push_worker_loop())

    async def disconnect(self) -> None:
        self._stopped = True
        if self._supervisor is not None:
            self._supervisor.cancel()
            try:
                await self._supervisor
            except asyncio.CancelledError:
                pass
            except Exception:  # защитная сетка отмены
                logger.debug("supervisor cancel", exc_info=True)
            self._supervisor = None
        if self._push_worker is not None:
            self._push_worker.cancel()
            try:
                await self._push_worker
            except asyncio.CancelledError:
                pass
            except Exception:  # защитная сетка отмены
                logger.debug("push worker cancel", exc_info=True)
            self._push_worker = None
        # Медиа: зависшие upload-ожидания падают сразу, HTTP-пул закрывается.
        self._waiter.fail_all(ConnectionError("max provider stopped"))
        if self._media is not None:
            await self._media.aclose()
        await self._client.close()
        # Завершаем incoming_stream (forward-таска корректно закончится).
        await self._incoming_queue.put(_STREAM_END)

    def is_connected(self) -> bool:
        return not self._dead and self._client.is_connected()

    def is_dead(self) -> bool:
        """Токен отозван: провайдера надо снять (AccountSyncWorker)."""
        return self._dead

    def credential_token(self) -> str | None:
        """Токен, на котором работает провайдер: расхождение с строкой
        аккаунта = менеджер перепривязался новым QR — провайдер надо
        пересобрать (AccountSyncWorker)."""
        return self._token

    async def incoming_stream(self) -> AsyncIterator[IncomingMessage]:
        while True:
            msg = await self._incoming_queue.get()
            if msg is None:
                break
            yield msg

    async def send_message(
        self, external_chat_id: str, text: str, *, is_initiation: bool
    ) -> SendResult:
        try:
            chat_id = int(external_chat_id)
        except ValueError:
            return SendResult(success=False, error=f"bad_chat_id: {external_chat_id!r}")
        if self._client.closed or self._dead:
            return SendResult(success=False, error="not connected")
        try:
            resp = await self._client.request(
                OP_MSG_SEND, msg_send_payload(chat_id, text, self._next_cid())
            )
        except Exception as exc:  # noqa: BLE001 - маппинг+лог в _send_exc_result
            return self._send_exc_result(exc)
        return self._send_result(resp)

    # ------------------------------------------------------------------ #
    # Медиа (контракт MessengerProvider)
    # ------------------------------------------------------------------ #
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
        """Upload → MSG_SEND с attaches. Вселенная ретраев — outbox: upload-URL
        одноразовый, каждая попытка заливает файл заново (свежий cid —
        дедуп MAX не съест повтор после сбоя)."""
        try:
            chat_id = int(external_chat_id)
        except ValueError:
            return SendResult(success=False, error=f"bad_chat_id: {external_chat_id!r}")
        if self._client.closed or self._dead or self._media is None:
            return SendResult(success=False, error="not connected")
        kind = _UPLOAD_KINDS.get(content_type, UPLOAD_KIND_FILE)
        try:
            resp = await asyncio.wait_for(
                self._send_media_inner(chat_id, kind, path, mime_type, file_name, caption),
                timeout=self._media_send_timeout,
            )
        except TimeoutError:  # вкл. «136 не пришёл» и висящий upload
            return SendResult(success=False, error="send_timeout")
        except Exception as exc:  # noqa: BLE001 - маппинг+лог в _send_exc_result
            return self._send_exc_result(exc)
        return self._send_result(resp)

    async def _send_media_inner(
        self,
        chat_id: int,
        kind: str,
        path: Path,
        mime_type: str | None,
        file_name: str | None,
        caption: str | None,
    ) -> dict:
        attaches = await self._media.upload(
            chat_id=chat_id, kind=kind, path=path, mime=mime_type, file_name=file_name
        )
        return await self._client.request(
            OP_MSG_SEND,
            msg_send_payload(chat_id, caption or "", self._next_cid(), attaches),
        )

    @staticmethod
    def _send_result(resp: dict) -> SendResult:
        msg = (resp.get("payload") or {}).get("message") or {}
        mid = msg.get("id")
        # id приходит ЧИСЛОМ (хотя в push'ах — строкой): храним как str;
        # str(None) дал бы литеральную строку "None" — проверяем явно.
        return SendResult(
            success=True,
            external_message_id=str(mid) if mid is not None else None,
        )

    def _send_exc_result(self, exc: Exception) -> SendResult:
        """Общий маппер ошибок отправки (текст и медиа)."""
        if isinstance(exc, MaxAuthError):
            self._dead = True
            logger.error("MAX токен отозван (send): %s", exc)
            return SendResult(success=False, error="max_auth")
        retry_after = getattr(exc, "retry_after_seconds", None)
        if retry_after:
            return SendResult(
                success=False, error="max_throttle", retry_after_seconds=int(retry_after)
            )
        logger.exception("MAX send failed")
        return SendResult(success=False, error=str(exc))

    # ------------------------------------------------------------------ #
    # Внутреннее
    # ------------------------------------------------------------------ #
    def _next_cid(self) -> int:
        """ms-таймстамп + счётчик: уникальный cid для дедупа при очереди."""
        return (int(time.time() * 1000) << 8) | (next(self._cid_counter) & 0xFF)

    async def _ws_request(
        self, opcode: int, payload: dict | None = None, *, timeout: float | None = None
    ) -> dict:
        """Запрос через ТЕКУЩИЙ клиент: реконнект подменяет ``self._client``,
        bound-method старого жил бы ConnectionError'ом вечно (HTTP-пул
        медиа переживает реконнект, WS-запросы — нет)."""
        return await self._client.request(opcode, payload, timeout=timeout)

    async def _safe_close_client(self) -> None:
        """Закрыть WS-клиент best-effort (он мог уже умереть сам).

        Зависшие upload-ожидания (push 136 больше не придёт по этому
        соединению) падают сразу — outbox-ретрай перезольёт файл с новым
        upload-URL. HTTP-пул медиа НЕ закрываем: он не связан с WS."""
        self._waiter.fail_all(ConnectionError("max ws closed"))
        try:
            await self._client.close()
        except Exception:
            logger.debug("MAX client close best-effort", exc_info=True)

    async def _connect_once(self) -> None:
        await self._client.connect()
        await self._client.request(
            OP_INIT,
            init_payload(self._device_id, self._user_agent),
            timeout=self._request_timeout,
        )
        await self._client.request(OP_LOGIN, login_payload(self._token), timeout=20.0)
        logger.info(
            "MAX online: device=%s… user_id=%s",
            self._device_id[:8],
            self._own_user_id,
        )

    async def _supervise_loop(self) -> None:
        """Реконнект с backoff + heartbeat. Умирает только на stop/auth-отказе."""
        backoff = self._backoff_min_sec
        while not self._stopped:
            try:
                if self._client.closed:
                    await self._connect_once()
                    backoff = self._backoff_min_sec  # LOGIN ok — сброс
                # Тишина >15с — шлём свой ping (авто-pong покрывает только
                # серверные пинги).
                if time.monotonic() - self._client.last_send > self._heartbeat_idle_sec:
                    await self._client.request(OP_PING, {"interactive": True}, timeout=10.0)
                await asyncio.sleep(self._heartbeat_tick_sec)
            except asyncio.CancelledError:
                raise
            except MaxAuthError as exc:
                self._dead = True
                await self._safe_close_client()
                logger.error("MAX токен отозван — провайдер мёртв (нужен новый QR): %s", exc)
                return
            except Exception as exc:  # noqa: BLE001 - реконнект переживает любой сбой
                # ГРАБЛЯ: если LOGIN упал при живом транспорте (throttle/
                # таймаут), НЕ закрыть клиент = вечно-«здоровый» зомби:
                # ping отвечает, is_connected() True, но сессии нет и push'и
                # не приходят. Закрываем — следующая итерация построит свежий
                # клиент с полным INIT+LOGIN (через фабрику-шов).
                await self._safe_close_client()
                logger.warning("MAX обрыв (%s) — реконнект через %.0fс", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self._backoff_max_sec)
                self._client = self._client_factory()
                self._client.on_push(self._on_push)

    async def _on_push(self, frame: dict) -> None:
        """Reader-колбэк: ТОЛЬКО разбор и складывание в очередь обогащения

        (никаких await request() — дедлок, см. __init__). Исключение —
        push 136 (готовность upload): синхронный resolve фьючерса."""
        if frame.get("opcode") == OP_UPLOAD_READY:
            self._waiter.feed(frame.get("payload") or {})
            return
        parsed = parse_message_push(frame, self._own_user_id)
        if parsed.skip_reason is not None:
            if parsed.skip_reason != "activity":
                logger.info("MAX push пропущен: %s", parsed.skip_reason)
            return
        self._push_queue.put_nowait(parsed)

    async def _push_worker_loop(self) -> None:
        while True:
            parsed = await self._push_queue.get()
            try:
                await self._process_push(parsed)
            except asyncio.CancelledError:
                raise
            except Exception:  # одна ошибка не убивает воркер
                logger.exception("MAX push-обработка упала (chat=%s)", parsed.external_chat_id)

    async def _process_push(self, parsed) -> None:
        assert parsed.content_type is not None
        chat_id = parsed.external_chat_id or ""
        if not parsed.chat_type_known and not await self._chat_is_dialog(chat_id):
            # Лёгкий пуш без chat.type; CHAT_INFO сказал «не DIALOG» (группа).
            logger.info("MAX push пропущен: group_chat_info chat=%s", chat_id)
            return
        name, phone, first_name, last_name = await self._resolve_sender(
            parsed.sender_external_id or ""
        )
        media = await self._download_media(parsed, chat_id)
        await self._incoming_queue.put(
            IncomingMessage(
                messenger=Messenger.max,
                external_chat_id=chat_id,
                sender_external_id=parsed.sender_external_id or "",
                sender_name=name,
                sender_phone=phone,
                sender_username=None,
                sender_first_name=first_name,
                sender_last_name=last_name,
                content_type=parsed.content_type,
                text=parsed.text,
                external_message_id=parsed.external_message_id,
                timestamp=parsed.timestamp,
                is_reply=parsed.is_reply,
                media=media,
            )
        )

    async def _download_media(self, parsed, chat_id: str) -> MediaPayload | None:
        """Скачать вложение входящего (eager, как у TG).

        Сбой/таймаут → None: в тексте уже стоит плейсхолдер от парсера,
        сообщение не теряется. Внимание: скачивание после группового
        гейта — медиа групповых чатов не тянем."""
        if self._media is None or parsed.attach is None:
            return None
        numeric = int(chat_id) if chat_id.isdigit() else 0
        try:
            return await asyncio.wait_for(
                self._media.download(
                    parsed.attach,
                    chat_id=numeric,
                    message_id=parsed.external_message_id,
                ),
                timeout=self._media_download_timeout,
            )
        except TimeoutError:
            logger.warning("MAX download таймаут (chat=%s) — плейсхолдер", chat_id)
            return None

    async def _chat_is_dialog(self, chat_id: str) -> bool:
        """Тип чата для лёгких push'ей (CHAT_INFO, кэш по chatId).

        При сбое запроса — fail-open (считаем диалогом и НЕ кэшируем):
        потерять сообщение клиента хуже, чем редкая утечка группового.
        """
        cached = self._chat_is_dialog_cache.get(chat_id)
        if cached is not None:
            return cached
        try:
            numeric = int(chat_id)
        except ValueError:
            return False  # нечисловой chatId — точно не наш кейс
        try:
            resp = await self._client.request(
                OP_CHAT_INFO,
                {"chatId": numeric},
                timeout=self._request_timeout,
            )
        except Exception as exc:  # noqa: BLE001 - enrichment best-effort
            logger.warning(
                "MAX CHAT_INFO не ответил (chat=%s): %s — считаем диалогом",
                chat_id,
                exc,
            )
            return True
        chat = (resp.get("payload") or {}).get("chat") or {}
        is_dialog = str(chat.get("type") or "").upper() == "DIALOG"
        self._chat_is_dialog_cache[chat_id] = is_dialog
        return is_dialog

    async def _resolve_sender(
        self, sender_external_id: str
    ) -> tuple[str | None, str | None, str | None, str | None]:
        """Имя+телефон+split-имя отправителя (GET_CONTACTS, кэш по userId).

        Best-effort: сбой → (None,)*4 без кэша — сообщение не теряем,
        попробуем снова на следующем сообщении.
        """
        try:
            uid = int(sender_external_id)
        except ValueError:
            return None, None, None, None
        cached = self._sender_cache.get(uid)
        if cached is not None:
            return cached
        try:
            resp = await self._client.request(
                OP_GET_CONTACTS,
                {"contactIds": [uid]},
                timeout=self._request_timeout,
            )
        except Exception as exc:  # noqa: BLE001 - enrichment best-effort
            logger.warning(
                "MAX GET_CONTACTS не ответил (user=%s): %s — имя не найдено",
                sender_external_id,
                exc,
            )
            return None, None, None, None
        contacts = (resp.get("payload") or {}).get("contacts") or []
        name = phone = first = last = None
        if isinstance(contacts, list) and contacts and isinstance(contacts[0], dict):
            name = contact_display_name(contacts[0])
            phone = contact_phone(contacts[0])
            first, last = contact_name_parts(contacts[0])
        self._sender_cache[uid] = (name, phone, first, last)
        return name, phone, first, last
