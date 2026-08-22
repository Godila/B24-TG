"""Socket.IO-клиент событий OpenWA (namespace ``/events``).

Транспорт живых событий WA-канала: message.received/sent/ack, session.*.
Протокол (спека 6.5): команды и события ходят по одному event-имени
``message``; подписка — flat-конверт ``{type: "subscribe", sessionId,
events}``, события приходят вложенными ``{type: "event", payload: {event,
sessionId, data}}``. Ключ — в handshake-auth (не в URL). Переподключение —
штатный реконнект python-socketio; подписка разрыв не переживает, поэтому
on-connect крюк подписывает сессию заново.

Для тестов: ``sio_factory`` подменяется фейком (connect/on/emit/disconnect,
атрибут ``connected``).
"""

import logging
from collections.abc import Callable

logger = logging.getLogger(__name__)

_NAMESPACE = "/events"

#: Минимальный набор событий провайдера (спека 6.5 «Subscribable events»;
#: webhook-only message.failed/session.reconnect_loop сюда не входят).
SUBSCRIBE_EVENTS = (
    "message.received",
    "message.sent",
    "message.ack",
    "session.status",
    "session.disconnected",
    "session.restriction",
)


def _default_sio_factory():
    import socketio

    return socketio.AsyncClient()


class WaEventClient:
    """Один клиент = одна сессия OpenWA (подписка session-scoped)."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        on_event: Callable[[dict], None],
        sio_factory=None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._on_event = on_event
        self._session_id: str | None = None
        self._sio_factory = sio_factory or _default_sio_factory
        self._sio = self._sio_factory()
        self._sio.on("message", self._on_message, namespace=_NAMESPACE)
        self._sio.on("connect", self._on_connect, namespace=_NAMESPACE)

    def is_connected(self) -> bool:
        return bool(getattr(self._sio, "connected", False))

    async def start(self, session_id: str) -> None:
        """Подключиться и подписаться на события сессии."""
        self._session_id = session_id
        await self._sio.connect(
            self._base_url,
            namespaces=[_NAMESPACE],
            auth={"apiKey": self._api_key},
        )
        await self._subscribe(session_id)

    async def stop(self) -> None:
        self._session_id = None
        # shutdown() гасит и реконнект-цикл (disconnect() трогает только
        # живой сокет — клиент в backoff остался бы зомби-таской).
        await self._sio.shutdown()

    async def _subscribe(self, session_id: str) -> None:
        await self._sio.emit(
            "message",
            {"type": "subscribe", "sessionId": session_id, "events": list(SUBSCRIBE_EVENTS)},
            namespace=_NAMESPACE,
        )

    def _on_message(self, msg) -> None:
        """Сырой кадр: reply (subscribed/pong/error) или живое событие."""
        if not isinstance(msg, dict) or msg.get("type") != "event":
            return
        payload = msg.get("payload")
        if isinstance(payload, dict):
            self._on_event(payload)

    async def _on_connect(self, *args) -> None:
        """(Ре)коннект сокета — подписка не пережила разрыв, шлём заново."""
        if self._session_id is not None:
            try:
                await self._subscribe(self._session_id)
            except Exception:
                logger.warning("WA resubscribe failed", exc_info=True)
