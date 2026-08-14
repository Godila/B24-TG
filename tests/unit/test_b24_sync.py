from unittest.mock import AsyncMock, MagicMock

import pytest

from app.b24.crm import ContactInfo, DealInfo
from app.b24.sync import OUTBOUND_COMMENT_PREFIX, Bitrix24Sync


def _make_sync(crm, im):
    token_mgr = AsyncMock()
    token = MagicMock()
    token.access_token = "tok"
    token_mgr.get_token = AsyncMock(return_value=token)
    return Bitrix24Sync(token_mgr=token_mgr, crm=crm, im=im)


@pytest.mark.asyncio
async def test_new_contact_creates_contact_and_deal_and_timeline():
    crm = AsyncMock()
    crm.find_contact_by_phone = AsyncMock(return_value=None)  # новый клиент
    crm.create_contact = AsyncMock(return_value=ContactInfo(id=77, name="Иван"))
    crm.create_deal = AsyncMock(return_value=DealInfo(id=100, title="TG: Иван"))
    crm.add_timeline_comment = AsyncMock(return_value=999)

    im = AsyncMock()
    im.notify_manager = AsyncMock(return_value=1)

    sync = _make_sync(crm, im)
    result = await sync.process_inbound(
        sender_name="Иван", sender_phone="+79991234567",
        message_text="Здравствуйте", assigned_b24_user_id=1,
    )

    crm.create_contact.assert_awaited_once()
    crm.create_deal.assert_awaited_once()
    crm.add_timeline_comment.assert_awaited_once()
    # Уведомление — только новому клиенту.
    im.notify_manager.assert_awaited_once()
    assert result.deal_id == 100
    assert result.contact_id == 77
    assert result.is_new is True
    assert result.timeline_comment_id == 999


@pytest.mark.asyncio
async def test_existing_contact_reuses_open_deal():
    """Существующий контакт: ищем его ОТКРЫТУЮ сделку и пишем комментарий
    в неё (раньше deal_id навсегда оставался None)."""
    crm = AsyncMock()
    crm.find_contact_by_phone = AsyncMock(return_value=ContactInfo(id=42, name="Иван"))
    crm.find_open_deal_for_contact = AsyncMock(
        return_value=DealInfo(id=100, title="Старая сделка")
    )
    crm.add_timeline_comment = AsyncMock(return_value=888)

    im = AsyncMock()

    sync = _make_sync(crm, im)
    result = await sync.process_inbound(
        sender_name="Иван", sender_phone="+79991234567",
        message_text="Ещё вопрос", assigned_b24_user_id=1,
    )

    crm.create_contact.assert_not_awaited()
    crm.create_deal.assert_not_awaited()
    crm.find_open_deal_for_contact.assert_awaited_once()
    # Комментарий — в найденную сделку, не в карточку контакта.
    crm.add_timeline_comment.assert_awaited_once()
    assert crm.add_timeline_comment.call_args.kwargs["entity_type"] == "deal"
    assert crm.add_timeline_comment.call_args.kwargs["entity_id"] == 100
    # Уже знакомый клиент — менеджера не дёргаем.
    im.notify_manager.assert_not_awaited()
    assert result.contact_id == 42
    assert result.deal_id == 100
    assert result.is_new is False


@pytest.mark.asyncio
async def test_existing_contact_without_open_deal_comments_into_contact():
    """Существующий контакт без открытых сделок: новую не создаём,
    комментарий — в карточку контакта, deal_id=None."""
    crm = AsyncMock()
    crm.find_contact_by_phone = AsyncMock(return_value=ContactInfo(id=42, name="Иван"))
    crm.find_open_deal_for_contact = AsyncMock(return_value=None)
    crm.add_timeline_comment = AsyncMock(return_value=887)

    im = AsyncMock()

    sync = _make_sync(crm, im)
    result = await sync.process_inbound(
        sender_name="Иван", sender_phone="+79991234567",
        message_text="Вопрос", assigned_b24_user_id=1,
    )

    crm.create_deal.assert_not_awaited()
    crm.add_timeline_comment.assert_awaited_once()
    assert crm.add_timeline_comment.call_args.kwargs["entity_type"] == "contact"
    im.notify_manager.assert_not_awaited()
    assert result.deal_id is None
    assert result.is_new is False


@pytest.mark.asyncio
async def test_process_outbound_comments_into_deal():
    crm = AsyncMock()
    crm.add_timeline_comment = AsyncMock(return_value=555)
    sync = _make_sync(crm, AsyncMock())

    comment_id = await sync.process_outbound(
        dialog_deal_id=100, dialog_entity_type="deal",
        contact_id=42, text="Ответ менеджера",
    )

    assert comment_id == 555
    crm.add_timeline_comment.assert_awaited_once()
    kwargs = crm.add_timeline_comment.call_args.kwargs
    assert kwargs["entity_type"] == "deal"
    assert kwargs["entity_id"] == 100
    assert kwargs["comment"] == OUTBOUND_COMMENT_PREFIX + "Ответ менеджера"


@pytest.mark.asyncio
async def test_process_outbound_falls_back_to_contact_card():
    """Нет сделки у диалога — комментарий в карточку контакта."""
    crm = AsyncMock()
    crm.add_timeline_comment = AsyncMock(return_value=556)
    sync = _make_sync(crm, AsyncMock())

    comment_id = await sync.process_outbound(
        dialog_deal_id=None, dialog_entity_type=None,
        contact_id=42, text="Ответ",
    )

    assert comment_id == 556
    kwargs = crm.add_timeline_comment.call_args.kwargs
    assert kwargs["entity_type"] == "contact"
    assert kwargs["entity_id"] == 42


@pytest.mark.asyncio
async def test_process_outbound_without_entities_returns_none():
    """Ни сделки, ни контакта — писать некуда, None без вызовов CRM."""
    crm = AsyncMock()
    sync = _make_sync(crm, AsyncMock())

    comment_id = await sync.process_outbound(
        dialog_deal_id=None, dialog_entity_type=None,
        contact_id=None, text="Ответ",
    )

    assert comment_id is None
    crm.add_timeline_comment.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_outbound_without_token_returns_none():
    token_mgr = AsyncMock()
    token_mgr.get_token = AsyncMock(return_value=None)
    crm = AsyncMock()
    sync = Bitrix24Sync(token_mgr=token_mgr, crm=crm, im=AsyncMock())

    comment_id = await sync.process_outbound(
        dialog_deal_id=1, dialog_entity_type="deal", contact_id=2, text="x",
    )

    assert comment_id is None
    crm.add_timeline_comment.assert_not_awaited()
