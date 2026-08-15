"""MaxQrLoginFlow: машина состояний 288→289→291 (+115) по скриптованному клиенту."""

import asyncio

import pytest

from app.messaging.max.login import MaxQrLoginFlow, QrFlowStatus
from app.messaging.max.protocol import (
    OP_QR_AUTH_LOGIN,
    OP_QR_AUTH_POLL,
    OP_QR_AUTH_REQUEST,
    OP_QR_PASSWORD,
    MaxQrExpiredError,
)


class ScriptedClient:
    """request() отвечает по скрипту: {opcode: [ответов]}."""

    def __init__(self, script: dict[int, list]):
        self.script = {op: list(resps) for op, resps in script.items()}
        self.requests: list[tuple[int, dict]] = []
        self.push_cb = None
        self._closed = True

    async def connect(self):
        self._closed = False

    async def close(self):
        self._closed = True

    def is_connected(self):
        return not self._closed

    @property
    def closed(self):
        return self._closed

    def on_push(self, cb):
        self.push_cb = cb

    async def request(self, opcode, payload=None, *, timeout=None):
        self.requests.append((opcode, payload or {}))
        if self.script.get(opcode):
            resp = self.script[opcode].pop(0)
            if isinstance(resp, Exception):
                raise resp
            return resp
        # дефолтные ответы
        if opcode in (6, 19, 1):  # INIT / LOGIN / PING
            return {"cmd": 1, "seq": 0, "opcode": opcode, "payload": {}}
        if opcode == OP_QR_AUTH_POLL:
            return {"cmd": 1, "seq": 0, "opcode": opcode,
                    "payload": {"status": {"expiresAt": 1}}}
        raise RuntimeError(f"unexpected opcode {opcode}")


def _qr_resp(n: int = 1) -> dict:
    return {"cmd": 1, "seq": 0, "opcode": OP_QR_AUTH_REQUEST, "payload": {
        "qrLink": f"https://max.ru/:auth/track-{n}",
        "trackId": f"track-{n}",
        "pollingInterval": 50,  # мс → поллинг каждые 0.05с в тесте
        "ttl": 120000,
    }}


def _login_resp() -> dict:
    return {"cmd": 1, "seq": 0, "opcode": OP_QR_AUTH_LOGIN, "payload": {
        "tokenAttrs": {"LOGIN": {"token": "An_test"}},
        "profile": {"contact": {"id": 401041669, "firstName": "Гео",
                                 "phones": [{"number": "+79310000000"}]}},
    }}


def _make_flow(client) -> MaxQrLoginFlow:
    return MaxQrLoginFlow(
        client=client,
        device_id="dev-1",
        user_agent={"appVersion": "26.8.4"},
        deadline_sec=1.0,
        poll_interval_sec=0.01,
    )


@pytest.mark.asyncio
async def test_success_flow():
    client = ScriptedClient({
        OP_QR_AUTH_REQUEST: [_qr_resp()],
        OP_QR_AUTH_POLL: [
            {"payload": {"status": {}}},
            {"payload": {"status": {"loginAvailable": True}}},
        ],
        OP_QR_AUTH_LOGIN: [_login_resp()],
    })
    flow = _make_flow(client)
    await flow.run()

    assert flow.status is QrFlowStatus.authorized
    assert flow.result is not None
    assert flow.result.token == "An_test"
    assert flow.result.device_id == "dev-1"
    assert flow.result.max_user_id == 401041669
    assert flow.result.name == "Гео"
    assert flow.result.phone == "+79310000000"
    assert flow.qr_link == "https://max.ru/:auth/track-1"
    # соединение флоу закрыто (короткоживущее)
    assert client.closed


@pytest.mark.asyncio
async def test_password_challenge_flow():
    client = ScriptedClient({
        OP_QR_AUTH_REQUEST: [_qr_resp()],
        OP_QR_AUTH_POLL: [
            {"payload": {"status": {"loginAvailable": True}}},
        ],
        OP_QR_AUTH_LOGIN: [
            {"payload": {"passwordChallenge": True}},
        ],
        OP_QR_PASSWORD: [_login_resp()],
    })
    flow = _make_flow(client)
    task = asyncio.create_task(flow.run())
    # ждём password_required
    for _ in range(200):
        if flow.status is QrFlowStatus.password_required:
            break
        await asyncio.sleep(0.01)
    assert flow.status is QrFlowStatus.password_required
    assert flow.submit_password("secret")
    await asyncio.wait_for(task, timeout=2)
    assert flow.status is QrFlowStatus.authorized
    # 115 отправлен с паролем
    pw_calls = [p for op, p in client.requests if op == OP_QR_PASSWORD]
    assert pw_calls == [{"trackId": "track-1", "password": "secret"}]


@pytest.mark.asyncio
async def test_submit_password_rejected_when_not_waiting():
    client = ScriptedClient({OP_QR_AUTH_REQUEST: [_qr_resp()]})
    flow = _make_flow(client)
    assert flow.submit_password("x") is False


@pytest.mark.asyncio
async def test_qr_expiry_regenerates():
    client = ScriptedClient({
        OP_QR_AUTH_REQUEST: [_qr_resp(1), _qr_resp(2)],
        OP_QR_AUTH_POLL: [
            MaxQrExpiredError("track.not.found"),  # первый QR истёк
            {"payload": {"status": {"loginAvailable": True}}},
        ],
        OP_QR_AUTH_LOGIN: [_login_resp()],
    })
    flow = _make_flow(client)
    await flow.run()

    assert flow.status is QrFlowStatus.authorized
    assert flow.qr_link == "https://max.ru/:auth/track-2"  # перерисован
    assert len([1 for op, _ in client.requests if op == OP_QR_AUTH_REQUEST]) == 2


@pytest.mark.asyncio
async def test_deadline_expires():
    client = ScriptedClient({OP_QR_AUTH_REQUEST: [_qr_resp()]})
    flow = _make_flow(client)
    await flow.run()
    assert flow.status is QrFlowStatus.expired
    assert flow.result is None
