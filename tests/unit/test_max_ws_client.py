"""MaxWsClient: seq-матчинг, cmd=3 → типизированная ошибка, авто-pong."""

import asyncio
import json

import pytest

from app.messaging.max.protocol import OP_PING, OP_QR_AUTH_REQUEST, MaxQrExpiredError
from app.messaging.max.ws_client import MaxWsClient


class FakeWebsocket:
    """async-итерация из очереди кадров; send пишет в sent."""

    def __init__(self):
        self.inbox: asyncio.Queue[str] = asyncio.Queue()
        self.sent: list[dict] = []
        self.closed = False

    def feed(self, frame: dict) -> None:
        self.inbox.put_nowait(json.dumps(frame))

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    async def close(self) -> None:
        self.closed = True

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        if self.closed:
            raise StopAsyncIteration
        try:
            return await asyncio.wait_for(self.inbox.get(), timeout=1.0)
        except TimeoutError:
            raise StopAsyncIteration from None


def _make_client(fake_ws: FakeWebsocket) -> MaxWsClient:
    async def connect_fn(url, headers):
        return fake_ws

    return MaxWsClient(
        url="wss://test", headers={"Origin": "https://web.max.ru"}, connect_fn=connect_fn
    )


async def _reply_last_seq(client_ws: FakeWebsocket, payload: dict, cmd: int = 1) -> None:
    """Ответить на последний отправленный кадр с тем же seq."""
    last = client_ws.sent[-1]
    client_ws.feed({"ver": 11, "cmd": cmd, "seq": last["seq"], "opcode": last["opcode"], "payload": payload})


@pytest.mark.asyncio
async def test_request_matches_seq():
    fake = FakeWebsocket()
    client = _make_client(fake)
    await client.connect()

    async def scenario():
        task = asyncio.create_task(client.request(OP_QR_AUTH_REQUEST))
        await asyncio.sleep(0.01)
        await _reply_last_seq(fake, {"qrLink": "https://max.ru/:auth/x", "trackId": "t1"})
        return await task

    resp = await asyncio.wait_for(scenario(), timeout=2)
    assert resp["payload"]["trackId"] == "t1"
    # seq начинается с 1
    assert fake.sent[0]["seq"] == 1
    await client.close()


@pytest.mark.asyncio
async def test_cmd3_raises_classified_error():
    fake = FakeWebsocket()
    client = _make_client(fake)
    await client.connect()

    async def scenario():
        task = asyncio.create_task(client.request(OP_QR_AUTH_REQUEST))
        await asyncio.sleep(0.01)
        await _reply_last_seq(fake, {"error": "track.not.found"}, cmd=3)
        return await task

    with pytest.raises(MaxQrExpiredError):
        await asyncio.wait_for(scenario(), timeout=2)
    await client.close()


@pytest.mark.asyncio
async def test_server_ping_gets_interactive_pong():
    fake = FakeWebsocket()
    pushes = []
    client = _make_client(fake)

    async def on_push(frame):
        pushes.append(frame)

    client.on_push(on_push)
    await client.connect()

    fake.feed({"ver": 11, "cmd": 0, "seq": 500, "opcode": OP_PING, "payload": {}})
    await asyncio.sleep(0.05)

    pongs = [f for f in fake.sent if f["opcode"] == OP_PING]
    assert len(pongs) == 1
    assert pongs[0]["payload"] == {"interactive": True}
    # push-колбэк для op=1 не зовётся (это транспортная забота)
    assert pushes == []
    await client.close()


@pytest.mark.asyncio
async def test_push_dispatched_to_callback():
    fake = FakeWebsocket()
    seen = []
    client = _make_client(fake)

    async def on_push(frame):
        seen.append(frame)

    client.on_push(on_push)
    await client.connect()
    fake.feed({"ver": 11, "cmd": 0, "seq": 501, "opcode": 128, "payload": {"chatId": 1}})
    await asyncio.sleep(0.05)
    assert len(seen) == 1
    assert seen[0]["opcode"] == 128
    await client.close()
