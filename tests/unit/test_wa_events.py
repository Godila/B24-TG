"""Юнит-тесты WaEventClient: handshake-auth, подписка, разбор конвертов,
переподписка на реконнекте."""

import inspect

from app.messaging.whatsapp.events import SUBSCRIBE_EVENTS, WaEventClient


class FakeSio:
    def __init__(self):
        self.connected = False
        self.handlers = {}
        self.emits = []
        self.connect_calls = []

    def on(self, event, handler, namespace=None):
        self.handlers[(event, namespace)] = handler

    async def connect(self, url, namespaces=None, auth=None):
        self.connect_calls.append((url, namespaces, auth))
        self.connected = True
        await self._fire("connect", namespace="/events")

    async def disconnect(self):
        self.connected = False

    async def shutdown(self):
        self.connected = False

    async def emit(self, event, data, namespace=None):
        self.emits.append((event, data, namespace))

    async def fire_message(self, msg):
        await self._fire("message", msg)

    async def _fire(self, event, data=None, namespace="/events"):
        handler = self.handlers.get((event, namespace))
        if handler is None:
            return
        result = handler(data) if data is not None else handler()
        if inspect.isawaitable(result):
            await result


def make_client():
    seen = []
    sio = FakeSio()

    def factory():
        return sio

    client = WaEventClient(
        base_url="http://openwa:2785/",
        api_key="k1",
        on_event=seen.append,
        sio_factory=factory,
    )
    return client, sio, seen


async def test_start_connects_with_auth_and_subscribes():
    client, sio, _ = make_client()
    await client.start("s1")
    url, namespaces, auth = sio.connect_calls[0]
    assert url == "http://openwa:2785"
    assert namespaces == ["/events"]
    assert auth == {"apiKey": "k1"}
    # подписка могла прилететь дважды (connect-крюк + явная после connect) —
    # это идемпотентно на стороне OpenWA
    subscribes = [e for e in sio.emits if e[1].get("type") == "subscribe"]
    assert subscribes, "subscribe не отправлен"
    body = subscribes[0][1]
    assert body["sessionId"] == "s1"
    assert body["events"] == list(SUBSCRIBE_EVENTS)


async def test_event_envelope_dispatched_reply_ignored():
    client, sio, seen = make_client()
    await client.start("s1")
    await sio.fire_message({"type": "subscribed", "sessionId": "s1"})
    assert seen == []
    payload = {
        "event": "message.received",
        "sessionId": "s1",
        "data": {"id": "m1", "body": "hi"},
    }
    await sio.fire_message({"type": "event", "payload": payload})
    assert seen == [payload]


async def test_reconnect_resubscribes():
    client, sio, _ = make_client()
    await client.start("s1")
    count = len([e for e in sio.emits if e[1].get("type") == "subscribe"])
    sio.connected = False  # разрыв
    await sio._fire("connect")  # python-socketio снова поднял соединение
    subscribes = [e for e in sio.emits if e[1].get("type") == "subscribe"]
    assert len(subscribes) > count


async def test_stop_disconnects_and_disables_resubscribe():
    client, sio, _ = make_client()
    await client.start("s1")
    await client.stop()
    assert sio.connected is False
    assert client.is_connected() is False
    count = len(sio.emits)
    await sio._fire("connect")  # поздний реконнект — сессии уже нет
    assert len(sio.emits) == count
