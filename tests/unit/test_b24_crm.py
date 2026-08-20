from unittest.mock import AsyncMock

import pytest

from app.b24.client import Bitrix24Error
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
    client.call = AsyncMock(return_value=77)
    svc = CrmService(client)
    result = await svc.create_contact(
        auth_token="t",
        name="Иван Петров",
        phone="+79991234567",
        assigned_by_id=1,
        source="telegram",
    )
    assert result.id == 77
    client.call.assert_awaited_once()
    call_kwargs = client.call.call_args
    # Классический crm.contact.add (crm.item.add молча теряет PHONE/IM).
    assert call_kwargs.args[0] == "crm.contact.add"
    fields = call_kwargs.kwargs["params"]["fields"]
    assert fields["NAME"] == "Иван Петров"
    assert fields["PHONE"] == [{"VALUE": "+79991234567", "VALUE_TYPE": "MOBILE"}]
    assert fields["SOURCE_ID"] == "TELEGRAM"
    assert "IM" not in fields and "LAST_NAME" not in fields


@pytest.mark.asyncio
async def test_create_contact_split_name_im_and_no_phone():
    """Split-имя → NAME/LAST_NAME, username → IM; пустой телефон не пишем."""
    client = AsyncMock()
    client.call = AsyncMock(return_value=78)
    svc = CrmService(client)
    result = await svc.create_contact(
        auth_token="t",
        name="Иван Петров",
        phone="",
        assigned_by_id=1,
        source=None,
        first_name="Иван",
        last_name="Петров",
        username="ivan_p",
    )
    assert result.id == 78
    assert result.name == "Иван Петров"
    fields = client.call.call_args.kwargs["params"]["fields"]
    assert fields["NAME"] == "Иван"
    assert fields["LAST_NAME"] == "Петров"
    assert fields["IM"] == [{"VALUE": "ivan_p", "VALUE_TYPE": "TELEGRAM"}]
    assert "PHONE" not in fields
    assert "SOURCE_ID" not in fields


@pytest.mark.asyncio
async def test_create_contact_bad_source_retries_without_source():
    """Неточного SOURCE_ID (нет в справочнике) — ретрай без источника."""
    client = AsyncMock()

    async def call(method, auth_token, params=None, **kw):
        if "SOURCE_ID" in params["fields"]:
            raise Bitrix24Error(
                code="ERROR_SOURCE_ID",
                description="SOURCE_ID is not found in dictionary",
            )
        return 79

    client.call = AsyncMock(side_effect=call)
    svc = CrmService(client)
    result = await svc.create_contact(
        auth_token="t",
        name="Тимур",
        phone="",
        assigned_by_id=1,
        source="MAX",
    )
    assert result.id == 79
    assert client.call.await_count == 2
    assert "SOURCE_ID" not in client.call.call_args.kwargs["params"]["fields"]


@pytest.mark.asyncio
async def test_create_contact_other_error_not_retried():
    """Чужая ошибка B24 — ретрая без источника быть не должно."""
    client = AsyncMock()
    client.call = AsyncMock(side_effect=Bitrix24Error(code="ACCESS_DENIED", description=""))
    svc = CrmService(client)
    with pytest.raises(Bitrix24Error):
        await svc.create_contact(
            auth_token="t",
            name="Иван",
            phone="+7999",
            assigned_by_id=1,
            source="telegram",
        )
    assert client.call.await_count == 1


@pytest.mark.asyncio
async def test_create_deal():
    client = AsyncMock()
    client.call = AsyncMock(return_value={"item": {"id": 100, "title": "Сделка"}})
    svc = CrmService(client)
    result = await svc.create_deal(
        auth_token="t",
        title="Иван Петров",
        contact_id=77,
        assigned_by_id=1,
        source="telegram",
    )
    assert result.id == 100
    call_kwargs = client.call.call_args
    assert call_kwargs.kwargs["params"]["entityTypeId"] == 2  # DEAL
    # Канал — в поле «Источник», а не в название.
    assert call_kwargs.kwargs["params"]["fields"]["SOURCE_ID"] == "TELEGRAM"
    assert call_kwargs.kwargs["params"]["fields"]["TITLE"] == "Иван Петров"


# --- Лиды (crm_mode=lead) ------------------------------------------------ #


@pytest.mark.asyncio
async def test_create_lead():
    client = AsyncMock()
    client.call = AsyncMock(return_value=55)
    svc = CrmService(client)
    result = await svc.create_lead(
        auth_token="t",
        title="Иван Петров",
        phone="+79991234567",
        assigned_by_id=1,
        source="telegram",
        first_name="Иван",
        last_name="Петров",
        username="ivan_p",
    )
    assert result.id == 55
    call_kwargs = client.call.call_args
    # Классический crm.lead.add (crm.item.add молча теряет PHONE/IM).
    assert call_kwargs.args[0] == "crm.lead.add"
    fields = call_kwargs.kwargs["params"]["fields"]
    assert fields["TITLE"] == "Иван Петров"
    assert fields["NAME"] == "Иван"
    assert fields["LAST_NAME"] == "Петров"
    assert fields["PHONE"] == [{"VALUE": "+79991234567", "VALUE_TYPE": "MOBILE"}]
    assert fields["IM"] == [{"VALUE": "ivan_p", "VALUE_TYPE": "TELEGRAM"}]
    assert fields["SOURCE_ID"] == "TELEGRAM"
    assert fields["OPENED"] == "Y"


@pytest.mark.asyncio
async def test_create_lead_bad_source_retries_without_source():
    client = AsyncMock()

    async def call(method, auth_token, params=None, **kw):
        if "SOURCE_ID" in params["fields"]:
            raise Bitrix24Error(
                code="ERROR_SOURCE_ID",
                description="SOURCE_ID is not found in dictionary",
            )
        return 56

    client.call = AsyncMock(side_effect=call)
    svc = CrmService(client)
    result = await svc.create_lead(
        auth_token="t",
        title="Тимур",
        phone="",
        assigned_by_id=1,
        source="MAX",
    )
    assert result.id == 56
    assert client.call.await_count == 2
    assert "SOURCE_ID" not in client.call.call_args.kwargs["params"]["fields"]


@pytest.mark.asyncio
async def test_find_reusable_lead_by_phone():
    """findbyComm отдаёт LEAD-иды; статусы дочитывает crm.lead.list — берём
    новейший не-CONVERTED/JUNK."""
    client = AsyncMock()
    client.call = AsyncMock(
        side_effect=[
            {"CONTACT": [9], "LEAD": [4, 7]},
            [{"ID": "7", "TITLE": "Лид 7"}, {"ID": "4", "TITLE": "Лид 4"}],
        ]
    )
    svc = CrmService(client)
    lead = await svc.find_reusable_lead_by_phone(auth_token="t", phone="+7999")
    assert lead is not None
    assert lead.id == 7
    assert client.call.await_count == 2
    params = client.call.call_args.kwargs["params"]
    assert client.call.call_args.args[0] == "crm.lead.list"
    assert params["filter"]["ID"] == [4, 7]
    assert params["filter"]["!@STATUS_ID"] == ["CONVERTED", "JUNK"]
    assert params["order"] == {"ID": "desc"}


@pytest.mark.asyncio
async def test_find_reusable_lead_by_phone_no_leads():
    """findbyComm нашёл только контакт — лида нет, второй вызов не нужен."""
    client = AsyncMock()
    client.call = AsyncMock(return_value={"CONTACT": [9]})
    svc = CrmService(client)
    assert await svc.find_reusable_lead_by_phone(auth_token="t", phone="+7999") is None
    assert client.call.await_count == 1


@pytest.mark.asyncio
async def test_find_reusable_lead_by_phone_empty_phone_no_calls():
    """Пустой телефон (MAX) — матчинг неаводёжнее дублей, вызовов нет."""
    client = AsyncMock()
    svc = CrmService(client)
    assert await svc.find_reusable_lead_by_phone(auth_token="t", phone="") is None
    client.call.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_lead_parses_status_and_contact():
    client = AsyncMock()
    client.call = AsyncMock(
        return_value={"ID": "55", "TITLE": "Л", "STATUS_ID": "converted", "CONTACT_ID": "42"}
    )
    svc = CrmService(client)
    lead = await svc.get_lead(auth_token="t", lead_id=55)
    assert lead is not None
    assert lead.status_id == "CONVERTED"
    assert lead.contact_id == 42

    client.call = AsyncMock(return_value={"ID": "55", "STATUS_ID": "NEW", "CONTACT_ID": ""})
    lead = await svc.get_lead(auth_token="t", lead_id=55)
    assert lead.contact_id is None

    client.call = AsyncMock(side_effect=RuntimeError("deleted"))
    assert await svc.get_lead(auth_token="t", lead_id=55) is None


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
        auth_token="t",
        entity_type="deal",
        entity_id=100,
        comment="Текст сообщения",
    )
    assert comment_id == 999
    call_kwargs = client.call.call_args
    assert call_kwargs.args[0] == "crm.timeline.comment.add"


@pytest.mark.asyncio
async def test_add_timeline_comment_with_files():
    """files → FILES=[[имя, base64]] в fields: файл попадает в карточку CRM."""
    client = AsyncMock()
    client.call = AsyncMock(return_value=1001)
    svc = CrmService(client)
    await svc.add_timeline_comment(
        auth_token="t",
        entity_type="deal",
        entity_id=7,
        comment="[фото]",
        files=[("photo.jpg", "QUJD")],
    )
    fields = client.call.call_args.kwargs["params"]["fields"]
    assert fields["FILES"] == [["photo.jpg", "QUJD"]]


@pytest.mark.asyncio
async def test_add_timeline_comment_without_files_has_no_files_key():
    client = AsyncMock()
    client.call = AsyncMock(return_value=1002)
    svc = CrmService(client)
    await svc.add_timeline_comment(auth_token="t", entity_type="deal", entity_id=7, comment="текст")
    assert "FILES" not in client.call.call_args.kwargs["params"]["fields"]


@pytest.mark.asyncio
async def test_get_contact_null_fields_regression():
    """B24 шлёт явные null для незаполненных NAME/LAST_NAME (поймано в проде:
    контакт «Тимур» без фамилии ронял каждый входящий CRM-синк конкатенацией
    str + None). Имя собирается только из непустых частей."""
    client = AsyncMock()
    client.call = AsyncMock(
        return_value={"ID": "9", "NAME": "Тимур", "LAST_NAME": None}
    )
    svc = CrmService(client)
    contact = await svc.get_contact(auth_token="t", contact_id=9)
    assert contact is not None
    assert contact.name == "Тимур"

    client.call = AsyncMock(return_value={"ID": "7", "NAME": None, "LAST_NAME": None})
    contact = await svc.get_contact(auth_token="t", contact_id=7)
    assert contact is not None
    assert contact.name is None

    client.call = AsyncMock(
        return_value={"ID": "3", "NAME": None, "LAST_NAME": "Азизов"}
    )
    contact = await svc.get_contact(auth_token="t", contact_id=3)
    assert contact.name == "Азизов"


@pytest.mark.asyncio
async def test_find_contact_by_phone_null_last_name_regression():
    client = AsyncMock()
    client.call = AsyncMock(
        side_effect=[
            {"CONTACT": [9]},
            {"ID": "9", "NAME": "Тимур", "LAST_NAME": None},
        ]
    )
    svc = CrmService(client)
    contact = await svc.find_contact_by_phone(auth_token="t", phone="+7999")
    assert contact is not None
    assert contact.name == "Тимур"
