"""Тесты спайк-скрипта scripts/verify_b24_methods.py (Plan 003, без сети).

Скрипты — не пакет, поэтому загружаем модуль через importlib по пути файла.
Шаги гоняем на моке Bitrix24Client.call с ответами по имени метода
(для item-методов — по (метод, entityTypeId)), без реальных вызовов.
"""
import importlib.util
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.b24.client import Bitrix24Error

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "verify_b24_methods.py"

ALL_METHODS = [
    "app.info",
    "crm.duplicate.findbycomm",
    "crm.item.add (contact)",
    "crm.item.add (deal)",
    "crm.timeline.comment.add",
    "im.message.add",
    "crm.item.delete (deal, cleanup)",
    "crm.item.delete (contact, cleanup)",
]


def load_script() -> Any:
    spec = importlib.util.spec_from_file_location("verify_b24_methods", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def make_client(responses: dict[Any, Any]) -> AsyncMock:
    """AsyncMock для Bitrix24Client.call; ключи — метод или (метод, entityTypeId)."""

    def key(method: str, params: dict | None) -> Any:
        params = params or {}
        if "entityTypeId" in params:
            return (method, params["entityTypeId"])
        return method

    async def fake_call(method: str, auth_token: str | None = None,
                        params: dict | None = None, **kwargs: Any) -> Any:
        k = key(method, params)
        if k not in responses:
            raise AssertionError(f"unexpected call: {method} params={params}")
        resp = responses[k]
        if isinstance(resp, Exception):
            raise resp
        return resp

    client = AsyncMock()
    client.call = AsyncMock(side_effect=fake_call)
    return client


def happy_responses() -> dict[Any, Any]:
    return {
        "app.info": {"ID": "1", "EMAIL": "admin@example.com"},
        "crm.duplicate.findbycomm": {"CONTACT": []},  # «не найдено» — тоже OK
        ("crm.item.add", 3): {"item": {"id": 101}},  # contact_id
        ("crm.item.add", 2): {"item": {"id": 202}},  # deal_id
        "crm.timeline.comment.add": 555,
        "im.message.add": 777,
        ("crm.item.delete", 2): True,
        ("crm.item.delete", 3): True,
    }


@pytest.mark.asyncio
async def test_happy_path_all_ok_with_cleanup(monkeypatch):
    script = load_script()
    monkeypatch.delenv("SPIKE_ADMIN_USER_ID", raising=False)
    client = make_client(happy_responses())

    results = await script.run_verification(client, token="test-token")

    assert [name for name, _, _ in results] == ALL_METHODS
    assert all(status == "OK" for _, status, _ in results)
    assert script.all_ok(results) is True
    # Сделка привязана к контакту, timeline — к сделке; очистка удаляет обоих.
    calls = client.call.call_args_list
    add_deal = calls[3].kwargs["params"]
    assert add_deal["fields"]["CONTACT_ID"] == 101
    assert calls[4].kwargs["params"]["fields"]["ENTITY_ID"] == 202
    assert calls[6].kwargs["params"] == {"entityTypeId": 2, "id": 202}
    assert calls[7].kwargs["params"] == {"entityTypeId": 3, "id": 101}
    # Токен пробрасывается в каждый вызов.
    assert all(c.kwargs["auth_token"] == "test-token" for c in calls)


@pytest.mark.asyncio
async def test_access_denied_on_contact_add_skips_dependents(monkeypatch):
    script = load_script()
    monkeypatch.delenv("SPIKE_ADMIN_USER_ID", raising=False)
    responses = happy_responses()
    responses[("crm.item.add", 3)] = Bitrix24Error(
        "ACCESS_DENIED", "Method not available on this plan"
    )
    client = make_client(responses)

    results = await script.run_verification(client, token="test-token")

    by_name = {name: (status, detail) for name, status, detail in results}
    assert len(results) == 8  # все шаги в таблице, включая пропущенные
    assert by_name["app.info"][0] == "OK"
    assert by_name["crm.item.add (contact)"] == (
        "ERROR", "ACCESS_DENIED: Method not available on this plan"
    )
    for dep in ("crm.item.add (deal)", "crm.timeline.comment.add",
                "crm.item.delete (deal, cleanup)",
                "crm.item.delete (contact, cleanup)"):
        assert by_name[dep] == ("ERROR", "skipped-dependency"), dep
    # Независимый IM-шаг выполняется, несмотря на ACCESS_DENIED в CRM.
    assert by_name["im.message.add"][0] == "OK"
    # Зависимые вызовы не делались вовсе: не было ни сделки, ни удаления.
    called = [c.args[0] for c in client.call.call_args_list]
    assert "crm.item.delete" not in called
    assert ("crm.item.add", 2) not in [
        (c.args[0], (c.kwargs.get("params") or {}).get("entityTypeId"))
        for c in client.call.call_args_list
    ]
    assert script.all_ok(results) is False


@pytest.mark.asyncio
async def test_skip_im_omits_im_step(monkeypatch):
    script = load_script()
    monkeypatch.delenv("SPIKE_ADMIN_USER_ID", raising=False)
    client = make_client(happy_responses())

    results = await script.run_verification(client, token="test-token", skip_im=True)

    names = [name for name, _, _ in results]
    assert names == [m for m in ALL_METHODS if m != "im.message.add"]
    assert all(status == "OK" for _, status, _ in results)
    assert "im.message.add" not in [
        c.args[0] for c in client.call.call_args_list
    ]
