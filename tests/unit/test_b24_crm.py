from unittest.mock import AsyncMock

import pytest

from app.b24.crm import CrmService


@pytest.mark.asyncio
async def test_find_contact_by_phone_found():
    client = AsyncMock()
    # Реальный ответ crm.duplicate.findbyComm: dict по типам сущностей.
    # Первый call — findbyComm, второй — crm.contact.get для имени.
    client.call = AsyncMock(
        side_effect=[
            {"CONTACT": [42]},  # найден контакт с ID=42
            {"ID": "42", "NAME": "Иван", "LAST_NAME": "Петров"},
        ]
    )
    svc = CrmService(client)
    contact = await svc.find_contact_by_phone(auth_token="t", phone="+79991234567")
    assert contact is not None
    assert contact.id == 42
    assert contact.name == "Иван Петров"


@pytest.mark.asyncio
async def test_find_contact_by_phone_not_found():
    client = AsyncMock()
    # CONTACT пуст → контакт не найден.
    client.call = AsyncMock(return_value={"CONTACT": []})
    svc = CrmService(client)
    contact = await svc.find_contact_by_phone(auth_token="t", phone="+79990000000")
    assert contact is None


@pytest.mark.asyncio
async def test_create_contact():
    client = AsyncMock()
    client.call = AsyncMock(return_value={"item": {"id": 77, "title": "Иван Петров"}})
    svc = CrmService(client)
    result = await svc.create_contact(
        auth_token="t", name="Иван Петров", phone="+79991234567",
        assigned_by_id=1, source="telegram",
    )
    assert result.id == 77
    client.call.assert_awaited_once()
    call_kwargs = client.call.call_args
    assert call_kwargs.args[0] == "crm.item.add"
    assert call_kwargs.kwargs["params"]["entityTypeId"] == 3  # CONTACT


@pytest.mark.asyncio
async def test_create_deal():
    client = AsyncMock()
    client.call = AsyncMock(return_value={"item": {"id": 100, "title": "Сделка"}})
    svc = CrmService(client)
    result = await svc.create_deal(
        auth_token="t", title="TG: Иван Петров", contact_id=77, assigned_by_id=1,
    )
    assert result.id == 100
    call_kwargs = client.call.call_args
    assert call_kwargs.kwargs["params"]["entityTypeId"] == 2  # DEAL


@pytest.mark.asyncio
async def test_find_open_deal_for_contact_found():
    client = AsyncMock()
    # crm.item.list возвращает items; order id desc — первый элемент и есть
    # новейшая открытая сделка.
    client.call = AsyncMock(
        return_value={"items": [{"id": "300", "title": "Сделка 3"}, {"id": "200"}]}
    )
    svc = CrmService(client)
    deal = await svc.find_open_deal_for_contact(auth_token="t", contact_id=42)
    assert deal is not None
    assert deal.id == 300
    assert deal.title == "Сделка 3"
    call_kwargs = client.call.call_args
    assert call_kwargs.args[0] == "crm.item.list"
    params = call_kwargs.kwargs["params"]
    assert params["entityTypeId"] == 2  # DEAL
    assert params["filter"] == {"CONTACT_ID": 42, "CLOSED": "N"}
    assert params["order"] == {"id": "desc"}


@pytest.mark.asyncio
async def test_find_open_deal_for_contact_not_found():
    client = AsyncMock()
    client.call = AsyncMock(return_value={"items": []})
    svc = CrmService(client)
    deal = await svc.find_open_deal_for_contact(auth_token="t", contact_id=42)
    assert deal is None


@pytest.mark.asyncio
async def test_add_timeline_comment():
    client = AsyncMock()
    client.call = AsyncMock(return_value=999)
    svc = CrmService(client)
    comment_id = await svc.add_timeline_comment(
        auth_token="t", entity_type="deal", entity_id=100, comment="Текст сообщения",
    )
    assert comment_id == 999
    call_kwargs = client.call.call_args
    assert call_kwargs.args[0] == "crm.timeline.comment.add"
