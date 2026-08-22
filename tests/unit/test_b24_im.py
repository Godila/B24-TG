from unittest.mock import AsyncMock

import pytest

from app.b24.im import ImService


@pytest.mark.asyncio
async def test_send_notification_wire_contract():
    """im.message.add + DIALOG_ID/MESSAGE, KEYBOARD только когда передан;
    id ответа — на выходе (нужен для im.message.delete)."""
    client = AsyncMock()
    client.call = AsyncMock(return_value=555)
    svc = ImService(client)
    msg_id = await svc.send_notification(
        auth_token="t", user_id=15, message="Новое сообщение"
    )
    assert msg_id == 555
    call_kwargs = client.call.call_args
    assert call_kwargs.args[0] == "im.message.add"
    params = call_kwargs.kwargs["params"]
    assert params["DIALOG_ID"] == 15
    assert params["MESSAGE"] == "Новое сообщение"
    assert "KEYBOARD" not in params  # без клавиатуры параметр не шлём

    kb = {"BUTTONS": [[{"TYPE": "link", "TEXT": "Отвечать не нужно", "LINK": "https://x"}]]}
    await svc.send_notification(auth_token="t", user_id=15, message="m", keyboard=kb)
    assert client.call.call_args.kwargs["params"]["KEYBOARD"] == kb


@pytest.mark.asyncio
async def test_delete_message_wire_contract():
    client = AsyncMock()
    client.call = AsyncMock(return_value=True)
    svc = ImService(client)
    await svc.delete_message(auth_token="t", message_id=555)
    call_kwargs = client.call.call_args
    assert call_kwargs.args[0] == "im.message.delete"
    assert call_kwargs.kwargs["params"] == {"MESSAGE_ID": 555}


@pytest.mark.asyncio
async def test_bind_event():
    client = AsyncMock()
    client.call = AsyncMock(return_value=True)
    svc = ImService(client)
    ok = await svc.bind_event(auth_token="t", event="onCrmDealAdd", handler="https://x/h")
    assert ok is True
    call_kwargs = client.call.call_args
    assert call_kwargs.args[0] == "event.bind"
