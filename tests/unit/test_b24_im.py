from unittest.mock import AsyncMock

import pytest

from app.b24.im import ImService


@pytest.mark.asyncio
async def test_notify_manager():
    client = AsyncMock()
    client.call = AsyncMock(return_value=555)
    svc = ImService(client)
    msg_id = await svc.notify_manager(auth_token="t", user_id=15, message="Новое сообщение")
    assert msg_id == 555
    call_kwargs = client.call.call_args
    assert call_kwargs.args[0] == "im.message.add"
    assert call_kwargs.kwargs["params"]["DIALOG_ID"] == 15
    assert call_kwargs.kwargs["params"]["MESSAGE"] == "Новое сообщение"


@pytest.mark.asyncio
async def test_bind_event():
    client = AsyncMock()
    client.call = AsyncMock(return_value=True)
    svc = ImService(client)
    ok = await svc.bind_event(auth_token="t", event="onCrmDealAdd", handler="https://x/h")
    assert ok is True
    call_kwargs = client.call.call_args
    assert call_kwargs.args[0] == "event.bind"
