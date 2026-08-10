from unittest.mock import AsyncMock, MagicMock

import pytest

from app.b24.crm import ContactInfo, DealInfo
from app.b24.sync import Bitrix24Sync


@pytest.mark.asyncio
async def test_new_contact_creates_contact_and_deal_and_timeline():
    token_mgr = AsyncMock()
    token = MagicMock()
    token.access_token = "tok"
    token_mgr.get_token = AsyncMock(return_value=token)

    crm = AsyncMock()
    crm.find_contact_by_phone = AsyncMock(return_value=None)  # новый клиент
    crm.create_contact = AsyncMock(return_value=ContactInfo(id=77, name="Иван"))
    crm.create_deal = AsyncMock(return_value=DealInfo(id=100, title="TG: Иван"))
    crm.add_timeline_comment = AsyncMock(return_value=999)

    im = AsyncMock()
    im.notify_manager = AsyncMock(return_value=1)

    sync = Bitrix24Sync(token_mgr=token_mgr, crm=crm, im=im)
    result = await sync.process_inbound(
        sender_name="Иван", sender_phone="+79991234567",
        message_text="Здравствуйте", assigned_b24_user_id=1,
    )

    crm.create_contact.assert_awaited_once()
    crm.create_deal.assert_awaited_once()
    crm.add_timeline_comment.assert_awaited_once()
    im.notify_manager.assert_awaited_once()
    assert result.deal_id == 100
    assert result.contact_id == 77
    assert result.is_new is True


@pytest.mark.asyncio
async def test_existing_contact_reuses_deal():
    token_mgr = AsyncMock()
    token = MagicMock()
    token.access_token = "tok"
    token_mgr.get_token = AsyncMock(return_value=token)

    crm = AsyncMock()
    crm.find_contact_by_phone = AsyncMock(return_value=ContactInfo(id=42, name="Иван"))
    crm.add_timeline_comment = AsyncMock(return_value=888)

    im = AsyncMock()

    sync = Bitrix24Sync(token_mgr=token_mgr, crm=crm, im=im)
    result = await sync.process_inbound(
        sender_name="Иван", sender_phone="+79991234567",
        message_text="Ещё вопрос", assigned_b24_user_id=1,
    )

    crm.create_contact.assert_not_awaited()
    crm.create_deal.assert_not_awaited()
    crm.add_timeline_comment.assert_awaited_once()
    assert result.contact_id == 42
    assert result.is_new is False
