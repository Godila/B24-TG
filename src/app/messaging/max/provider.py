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

from app.messaging.max.protocol import (
    OP_INIT,
    OP_LOGIN,
    OP_MSG_SEND,
    OP_PING,
    MaxAuthError,
    init_payload,
    login_payload,
    msg_send_payload,
)
from app.messaging.max.push_parser import parse_message_push
from app.messaging.max.ws_client import MaxWsClient
from app.messaging.provider import MessengerProvider
from app.messaging.types import IncomingMessage, SendResult
from app.models import Messenger

logger = logging.getLogger(__name__)

#: Сигнал конца incoming_stream при disconnect() (forward-таска завершается).
_STREAM_END: IncomingMessage | None = None


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
        # Один seam на все случаи (первое подключение И реконнекты) —
        # тесты подменяют фабрику целиком, реальной сети в юнит-тестах нет.
        self._client_factory = client_factory or (
            lambda: MaxWsClient(
                url=ws_url, headers=headers, request_timeout=request_timeout
            )
        )
        self._client = self._client_factory()
        self._client.on_push(self._on_push)

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
        except MaxAuthError as exc:
            self._dead = True
            logger.error("MAX токен отозван (send): %s", exc)
            return SendResult(success=False, error="max_auth")
        except Exception as exc:
            retry_after = getattr(exc, "retry_after_seconds", None)
            if retry_after:
                return SendResult(
                    success=False,
                    error="max_throttle",
                    retry_after_seconds=int(retry_after),
                )
            logger.exception("MAX send_message failed")
            return SendResult(success=False, error=str(exc))
        msg = (resp.get("payload") or {}).get("message") or {}
        mid = msg.get("id")
        # id приходит ЧИСЛОМ (хотя в push'ах — строкой): храним как str;
        # str(None) дал бы литеральную строку "None" — проверяем явно.
        return SendResult(
            success=True,
            external_message_id=str(mid) if mid is not None else None,
        )

    # ------------------------------------------------------------------ #
    # Внутреннее
    # ------------------------------------------------------------------ #
    def _next_cid(self) -> int:
        """ms-таймстамп + счётчик: уникальный cid для дедупа при очереди."""
        return (int(time.time() * 1000) << 8) | (next(self._cid_counter) & 0xFF)

    async def _safe_close_client(self) -> None:
        """Закрыть WS-кли best-effort (он мог уже умереть сам)."""
        try:
            await self._client.close()
        except Exception:
            logger.debug("MAX client close best-effort", exc_info=True)

    async def _connect_once(self) -> None:
        await self._client.connect()
        await self._client.request(
            OP_INIT, init_payload(self._device_id, self._user_agent),
            timeout=self._request_timeout,
        )
        await self._client.request(
            OP_LOGIN, login_payload(self._token), timeout=20.0
        )
        logger.info(
            "MAX online: device=%s… user_id=%s",
            self._device_id[:8], self._own_user_id,
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
                    await self._client.request(
                        OP_PING, {"interactive": True}, timeout=10.0
                    )
                await asyncio.sleep(self._heartbeat_tick_sec)
            except asyncio.CancelledError:
                raise
            except MaxAuthError as exc:
                self._dead = True
                await self._safe_close_client()
                logger.error(
                    "MAX токен отозван — провайдер мёртв (нужен новый QR): %s", exc
                )
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
        parsed = parse_message_push(frame, self._own_user_id)
        if parsed.skip_reason is not None:
            if parsed.skip_reason != "activity":
                logger.info("MAX push пропущен: %s", parsed.skip_reason)
            return
        assert parsed.content_type is not None
        await self._incoming_queue.put(
            IncomingMessage(
                messenger=Messenger.max,
                external_chat_id=parsed.external_chat_id or "",
                sender_external_id=parsed.sender_external_id or "",
                sender_name=None,  # в push имён нет; контакт получит имя в CRM
                sender_phone=None,
                sender_username=None,
                content_type=parsed.content_type,
                text=parsed.text,
                external_message_id=parsed.external_message_id,
                timestamp=parsed.timestamp,
                is_reply=parsed.is_reply,
            )
        )
