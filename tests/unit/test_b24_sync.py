from unittest.mock import AsyncMock, MagicMock

import pytest

from app.b24.crm import ContactInfo, DealInfo, LeadInfo
from app.b24.sync import OUTBOUND_COMMENT_PREFIX, Bitrix24Sync
from app.models import Messenger


def _make_sync(crm):
    token_mgr = AsyncMock()
    token = MagicMock()
    token.access_token = "tok"
    token_mgr.get_token = AsyncMock(return_value=token)
    return Bitrix24Sync(token_mgr=token_mgr, crm=crm)


@pytest.mark.asyncio
async def test_new_contact_channel_data_passed_to_create():
    """Split-имя и @username доходят до create_contact (для NAME/LAST_NAME/IM)."""
    crm = AsyncMock()
    crm.find_contact_by_phone = AsyncMock(return_value=None)
    crm.create_contact = AsyncMock(return_value=ContactInfo(id=77, name="Иван"))
    crm.create_deal = AsyncMock(return_value=DealInfo(id=100, title="Иван"))
    crm.add_timeline_comment = AsyncMock(return_value=999)

    sync = _make_sync(crm)
    await sync.process_inbound(
        sender_name="Иван Петров",
        sender_phone="+79991234567",
        message_text="Здравствуйте",
        assigned_b24_user_id=1,
        sender_first_name="Иван",
        sender_last_name="Петров",
        sender_username="ivan_p",
    )
    kwargs = crm.create_contact.call_args.kwargs
    assert kwargs["first_name"] == "Иван"
    assert kwargs["last_name"] == "Петров"
    assert kwargs["username"] == "ivan_p"


@pytest.mark.asyncio
async def test_new_contact_creates_contact_and_deal_and_timeline():
    crm = AsyncMock()
    crm.find_contact_by_phone = AsyncMock(return_value=None)  # новый клиент
    crm.create_contact = AsyncMock(return_value=ContactInfo(id=77, name="Иван"))
    crm.create_deal = AsyncMock(return_value=DealInfo(id=100, title="Иван"))
    crm.add_timeline_comment = AsyncMock(return_value=999)


    sync = _make_sync(crm)
    result = await sync.process_inbound(
        sender_name="Иван",
        sender_phone="+79991234567",
        message_text="Здравствуйте",
        assigned_b24_user_id=1,
    )

    crm.create_contact.assert_awaited_once()
    crm.create_deal.assert_awaited_once()
    crm.add_timeline_comment.assert_awaited_once()
    # Название сделки — имя клиента (канал уходит в SOURCE_ID, не в TITLE).
    deal_kwargs = crm.create_deal.call_args.kwargs
    assert deal_kwargs["title"] == "Иван"
    assert deal_kwargs["source"] == "telegram"
    assert result.crm_entity_type == "deal"
    assert result.crm_entity_id == 100
    assert result.contact_id == 77
    assert result.is_new is True
    assert result.timeline_comment_id == 999


@pytest.mark.asyncio
async def test_existing_contact_reuses_open_deal():
    """Существующий контакт: ищем его ОТКРЫТУЮ сделку и пишем комментарий
    в неё (раньше deal_id навсегда оставался None)."""
    crm = AsyncMock()
    crm.find_contact_by_phone = AsyncMock(return_value=ContactInfo(id=42, name="Иван"))
    crm.find_open_deal_for_contact = AsyncMock(return_value=DealInfo(id=100, title="Старая сделка"))
    crm.add_timeline_comment = AsyncMock(return_value=888)


    sync = _make_sync(crm)
    result = await sync.process_inbound(
        sender_name="Иван",
        sender_phone="+79991234567",
        message_text="Ещё вопрос",
        assigned_b24_user_id=1,
        timeline_mode="all",
    )

    crm.create_contact.assert_not_awaited()
    crm.create_deal.assert_not_awaited()
    crm.find_open_deal_for_contact.assert_awaited_once()
    # Комментарий — в найденную сделку, не в карточку контакта.
    crm.add_timeline_comment.assert_awaited_once()
    assert crm.add_timeline_comment.call_args.kwargs["entity_type"] == "deal"
    assert crm.add_timeline_comment.call_args.kwargs["entity_id"] == 100
    assert result.contact_id == 42
    assert result.crm_entity_id == 100
    assert result.is_new is False


@pytest.mark.asyncio
async def test_existing_contact_without_open_deal_comments_into_contact():
    """Существующий контакт без открытых сделок: новую не создаём,
    комментарий — в карточку контакта, deal_id=None."""
    crm = AsyncMock()
    crm.find_contact_by_phone = AsyncMock(return_value=ContactInfo(id=42, name="Иван"))
    crm.find_open_deal_for_contact = AsyncMock(return_value=None)
    crm.add_timeline_comment = AsyncMock(return_value=887)


    sync = _make_sync(crm)
    result = await sync.process_inbound(
        sender_name="Иван",
        sender_phone="+79991234567",
        message_text="Вопрос",
        assigned_b24_user_id=1,
        timeline_mode="all",
    )

    crm.create_deal.assert_not_awaited()
    crm.add_timeline_comment.assert_awaited_once()
    assert crm.add_timeline_comment.call_args.kwargs["entity_type"] == "contact"
    assert result.crm_entity_id is None
    assert result.is_new is False


@pytest.mark.asyncio
async def test_process_outbound_comments_into_deal():
    crm = AsyncMock()
    crm.add_timeline_comment = AsyncMock(return_value=555)
    sync = _make_sync(crm)

    comment_id = await sync.process_outbound(
        dialog_entity_id=100,
        dialog_entity_type="deal",
        contact_id=42,
        text="Ответ менеджера",
        timeline_mode="all",
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
    sync = _make_sync(crm)

    comment_id = await sync.process_outbound(
        dialog_entity_id=None,
        dialog_entity_type=None,
        contact_id=42,
        text="Ответ",
        timeline_mode="all",
    )

    assert comment_id == 556
    kwargs = crm.add_timeline_comment.call_args.kwargs
    assert kwargs["entity_type"] == "contact"
    assert kwargs["entity_id"] == 42


@pytest.mark.asyncio
async def test_process_outbound_without_entities_returns_none():
    """Ни сделки, ни контакта — писать некуда, None без вызовов CRM."""
    crm = AsyncMock()
    sync = _make_sync(crm)

    comment_id = await sync.process_outbound(
        dialog_entity_id=None,
        dialog_entity_type=None,
        contact_id=None,
        text="Ответ",
    )

    assert comment_id is None
    crm.add_timeline_comment.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_outbound_without_token_returns_none():
    token_mgr = AsyncMock()
    token_mgr.get_token = AsyncMock(return_value=None)
    crm = AsyncMock()
    sync = Bitrix24Sync(token_mgr=token_mgr, crm=crm)

    comment_id = await sync.process_outbound(
        dialog_entity_id=1,
        dialog_entity_type="deal",
        contact_id=2,
        text="x",
    )

    assert comment_id is None
    crm.add_timeline_comment.assert_not_awaited()


# --- Режимы таймлайна (app_settings.timeline_mode) ---------------------- #


def _crm_new_contact():
    crm = AsyncMock()
    crm.find_contact_by_phone = AsyncMock(return_value=None)
    crm.create_contact = AsyncMock(return_value=ContactInfo(id=77, name="Иван"))
    crm.create_deal = AsyncMock(return_value=DealInfo(id=100, title="Иван"))
    crm.add_timeline_comment = AsyncMock(return_value=999)
    return crm


def _crm_existing_contact():
    crm = AsyncMock()
    crm.find_contact_by_phone = AsyncMock(return_value=ContactInfo(id=42, name="Иван"))
    crm.find_open_deal_for_contact = AsyncMock(return_value=DealInfo(id=100, title="Старая"))
    crm.add_timeline_comment = AsyncMock(return_value=888)
    return crm


@pytest.mark.asyncio
async def test_timeline_mode_first_only_first_message_commented():
    # Первое сообщение нового диалога — комментарий с маркером.
    sync = _make_sync(_crm_new_contact())
    result = await sync.process_inbound(
        sender_name="Иван",
        sender_phone="+79991234567",
        message_text="Здравствуйте",
        assigned_b24_user_id=1,
        timeline_mode="first",
    )
    sync._crm.add_timeline_comment.assert_awaited_once()
    text = sync._crm.add_timeline_comment.call_args.kwargs["comment"]
    assert text.startswith("💬 Диалог открыт")
    assert "Здравствуйте" in text
    assert result.timeline_comment_id == 999

    # Второе сообщение (контакт уже известен) — без комментария.
    sync2 = _make_sync(_crm_existing_contact())
    result2 = await sync2.process_inbound(
        sender_name="Иван",
        sender_phone="+79991234567",
        message_text="Ещё вопрос",
        assigned_b24_user_id=1,
        timeline_mode="first",
    )
    sync2._crm.add_timeline_comment.assert_not_awaited()
    assert result2.timeline_comment_id is None


@pytest.mark.asyncio
async def test_timeline_mode_none_no_comments():
    sync = _make_sync(_crm_new_contact())
    result = await sync.process_inbound(
        sender_name="Иван",
        sender_phone="+7",
        message_text="привет",
        assigned_b24_user_id=1,
        timeline_mode="none",
    )
    sync._crm.add_timeline_comment.assert_not_awaited()
    assert result.timeline_comment_id is None
    # Контакт и сделка всё равно создаются.
    sync._crm.create_contact.assert_awaited_once()


@pytest.mark.asyncio
async def test_timeline_mode_all_comment_on_every_message():
    sync = _make_sync(_crm_existing_contact())
    await sync.process_inbound(
        sender_name="Иван",
        sender_phone="+7",
        message_text="второе сообщение",
        assigned_b24_user_id=1,
        timeline_mode="all",
    )
    sync._crm.add_timeline_comment.assert_awaited_once()
    # Без маркера — обычный текст.
    assert sync._crm.add_timeline_comment.call_args.kwargs["comment"] == "второе сообщение"


@pytest.mark.asyncio
async def test_outbound_timeline_mode_first_and_none_skip_comment():
    for mode in ("first", "none"):
        token_mgr = AsyncMock()
        token = MagicMock()
        token.access_token = "tok"
        token_mgr.get_token = AsyncMock(return_value=token)
        crm = AsyncMock()
        sync = Bitrix24Sync(token_mgr=token_mgr, crm=crm)
        result = await sync.process_outbound(
            dialog_entity_id=100,
            dialog_entity_type="deal",
            contact_id=42,
            text="ответ",
            timeline_mode=mode,
        )
        assert result is None
        crm.add_timeline_comment.assert_not_awaited()


# --- Режим «Лиды» (app_settings.crm_mode) -------------------------------- #


def _crm_new_lead():
    crm = AsyncMock()
    crm.find_reusable_lead_by_phone = AsyncMock(return_value=None)
    crm.create_lead = AsyncMock(return_value=LeadInfo(id=55))
    crm.add_timeline_comment = AsyncMock(return_value=777)
    return crm


@pytest.mark.asyncio
async def test_lead_mode_new_client_creates_only_lead():
    """Новый клиент в lead-режиме = только лид: без контакта и сделки,
    комментарий в карточку лида."""
    crm = _crm_new_lead()
    sync = _make_sync(crm)
    result = await sync.process_inbound(
        sender_name="Иван",
        sender_phone="+79991234567",
        message_text="Здравствуйте",
        assigned_b24_user_id=1,
        crm_mode="lead",
    )

    crm.create_lead.assert_awaited_once()
    lead_kwargs = crm.create_lead.call_args.kwargs
    assert lead_kwargs["title"] == "Иван"  # канал — в SOURCE_ID, не в название
    assert lead_kwargs["source"] == "telegram"
    assert lead_kwargs["phone"] == "+79991234567"
    crm.create_contact.assert_not_awaited()
    crm.create_deal.assert_not_awaited()
    crm.add_timeline_comment.assert_awaited_once()
    comment_kwargs = crm.add_timeline_comment.call_args.kwargs
    assert comment_kwargs["entity_type"] == "lead"
    assert comment_kwargs["entity_id"] == 55
    assert result.crm_entity_type == "lead"
    assert result.crm_entity_id == 55
    assert result.contact_id is None
    assert result.is_new is True


@pytest.mark.asyncio
async def test_lead_mode_reusable_lead_no_create_no_notify():
    """Телефон матчится на живой лид — переиспользуем, ничего не создаём."""
    crm = AsyncMock()
    crm.find_reusable_lead_by_phone = AsyncMock(return_value=LeadInfo(id=7))
    crm.add_timeline_comment = AsyncMock(return_value=778)
    sync = _make_sync(crm)
    result = await sync.process_inbound(
        sender_name="Иван",
        sender_phone="+7999",
        message_text="Ещё вопрос",
        assigned_b24_user_id=1,
        crm_mode="lead",
        timeline_mode="all",
    )

    crm.create_lead.assert_not_awaited()
    assert crm.add_timeline_comment.call_args.kwargs["entity_id"] == 7
    assert result.crm_entity_id == 7
    assert result.is_new is False


@pytest.mark.asyncio
async def test_lead_mode_bound_live_lead_reused():
    """Живая привязка диалога к лиду авторитетнее поиска по телефону."""
    crm = AsyncMock()
    crm.get_lead = AsyncMock(return_value=LeadInfo(id=5, status_id="IN_PROCESS"))
    crm.add_timeline_comment = AsyncMock(return_value=779)
    sync = _make_sync(crm)
    result = await sync.process_inbound(
        sender_name="Иван",
        sender_phone="+7999",
        message_text="Привет снова",
        assigned_b24_user_id=1,
        existing_entity_id=5,
        existing_entity_type="lead",
        timeline_mode="all",
    )

    crm.get_lead.assert_awaited_once()
    crm.find_reusable_lead_by_phone.assert_not_awaited()
    assert crm.add_timeline_comment.call_args.kwargs["entity_type"] == "lead"
    assert result.crm_entity_id == 5
    assert result.is_new is False


@pytest.mark.asyncio
async def test_lead_mode_converted_lead_rebinds_to_deal():
    """Сконвертированный лид → ребинд: контакт конвертации → его открытая
    сделка; комментарий уходит в сделку, диалог перепривяжется."""
    crm = AsyncMock()
    crm.get_lead = AsyncMock(
        return_value=LeadInfo(id=55, status_id="CONVERTED", contact_id=42)
    )
    crm.find_open_deal_for_contact = AsyncMock(return_value=DealInfo(id=300))
    crm.add_timeline_comment = AsyncMock(return_value=780)
    sync = _make_sync(crm)
    result = await sync.process_inbound(
        sender_name="Иван",
        sender_phone="+7999",
        message_text="После конвертации",
        assigned_b24_user_id=1,
        existing_entity_id=55,
        existing_entity_type="lead",
        timeline_mode="all",
    )

    crm.find_open_deal_for_contact.assert_awaited_once()
    assert crm.find_open_deal_for_contact.call_args.args[1] == 42
    comment_kwargs = crm.add_timeline_comment.call_args.kwargs
    assert comment_kwargs["entity_type"] == "deal"
    assert comment_kwargs["entity_id"] == 300
    assert result.crm_entity_type == "deal"
    assert result.crm_entity_id == 300
    assert result.contact_id == 42  # контакт конвертации — тоже в связку
    assert result.is_new is False


@pytest.mark.asyncio
async def test_lead_mode_converted_without_open_deal_stays_on_lead():
    """Конвертация без открытой сделки — остаёмся на лиде, найденный
    контакт всё равно сохраняется в связку."""
    crm = AsyncMock()
    crm.get_lead = AsyncMock(
        return_value=LeadInfo(id=55, status_id="CONVERTED", contact_id=42)
    )
    crm.find_open_deal_for_contact = AsyncMock(return_value=None)
    crm.add_timeline_comment = AsyncMock(return_value=781)
    sync = _make_sync(crm)
    result = await sync.process_inbound(
        sender_name="Иван",
        sender_phone="+7999",
        message_text="Вопрос",
        assigned_b24_user_id=1,
        existing_entity_id=55,
        existing_entity_type="lead",
        timeline_mode="all",
    )

    assert crm.add_timeline_comment.call_args.kwargs["entity_type"] == "lead"
    assert result.crm_entity_type == "lead"
    assert result.crm_entity_id == 55
    assert result.contact_id == 42


@pytest.mark.asyncio
async def test_lead_mode_converted_contact_fallback_findbycomm():
    """У сконвертированного лида CONTACT_ID пуст — контакт ищем по телефону."""
    crm = AsyncMock()
    crm.get_lead = AsyncMock(
        return_value=LeadInfo(id=55, status_id="CONVERTED", contact_id=None)
    )
    crm.find_contact_by_phone = AsyncMock(return_value=ContactInfo(id=42))
    crm.find_open_deal_for_contact = AsyncMock(return_value=DealInfo(id=300))
    crm.add_timeline_comment = AsyncMock(return_value=782)
    sync = _make_sync(crm)
    result = await sync.process_inbound(
        sender_name="Иван",
        sender_phone="+7999",
        message_text="После конвертации",
        assigned_b24_user_id=1,
        existing_entity_id=55,
        existing_entity_type="lead",
        timeline_mode="all",
    )

    crm.find_contact_by_phone.assert_awaited_once()
    assert result.crm_entity_type == "deal"
    assert result.crm_entity_id == 300


@pytest.mark.asyncio
async def test_lead_mode_deleted_lead_searches_by_phone():
    """Привязанный лид удалён в B24 — связка протухла, ищем заново."""
    crm = AsyncMock()
    crm.get_lead = AsyncMock(return_value=None)
    crm.find_reusable_lead_by_phone = AsyncMock(return_value=LeadInfo(id=8))
    crm.add_timeline_comment = AsyncMock(return_value=783)
    sync = _make_sync(crm)
    result = await sync.process_inbound(
        sender_name="Иван",
        sender_phone="+7999",
        message_text="Вопрос",
        assigned_b24_user_id=1,
        existing_entity_id=55,
        existing_entity_type="lead",
        timeline_mode="all",
    )

    crm.find_reusable_lead_by_phone.assert_awaited_once()
    assert result.crm_entity_id == 8


@pytest.mark.asyncio
async def test_deal_bound_dialog_wins_over_lead_mode():
    """Диалог, привязанный к сделке (deal-эра), и при режиме «Лиды»
    продолжает писать в сделку — режим не перекладывает существующие диалоги."""
    crm = AsyncMock()
    crm.get_contact = AsyncMock(return_value=ContactInfo(id=42))
    crm.add_timeline_comment = AsyncMock(return_value=784)
    sync = _make_sync(crm)
    result = await sync.process_inbound(
        sender_name="Иван",
        sender_phone="+7999",
        message_text="Вопрос",
        assigned_b24_user_id=1,
        existing_contact_id=42,
        existing_entity_id=100,
        existing_entity_type="deal",
        crm_mode="lead",
        timeline_mode="all",
    )

    crm.get_lead.assert_not_awaited()
    crm.find_reusable_lead_by_phone.assert_not_awaited()
    assert crm.add_timeline_comment.call_args.kwargs["entity_type"] == "deal"
    assert result.crm_entity_id == 100


@pytest.mark.asyncio
async def test_stale_deal_binding_follows_lead_mode():
    """Прод-кейс 2026-08-20: контакт удалён в B24, диалог ещё несёт
    deal-привязку с прошлой эры, режим панели — «Лиды». Клиент фактически
    новый → карточку заводит РЕЖИМ (лид), а не тип протухшей привязки."""
    crm = AsyncMock()
    crm.get_contact = AsyncMock(return_value=None)  # контакт 15 удалён в B24
    crm.find_contact_by_phone = AsyncMock(return_value=None)  # по телефону пусто
    crm.create_lead = AsyncMock(return_value=LeadInfo(id=60))
    crm.add_timeline_comment = AsyncMock(return_value=785)
    sync = _make_sync(crm)
    result = await sync.process_inbound(
        sender_name="Гость",
        sender_phone="+7999",
        message_text="Тест лида",
        assigned_b24_user_id=1,
        existing_contact_id=15,
        existing_entity_id=11,
        existing_entity_type="deal",  # протухшая deal-привязка диалога
        crm_mode="lead",
    )

    crm.create_lead.assert_awaited_once()
    crm.create_contact.assert_not_awaited()
    crm.create_deal.assert_not_awaited()
    assert result.crm_entity_type == "lead"
    assert result.crm_entity_id == 60
    assert result.is_new is True


@pytest.mark.asyncio
async def test_stale_lead_binding_follows_deal_mode():
    """Симметрия: лид удалён в B24, диалог lead-привязан, режим — «Сделки».
    Клиент фактически новый → создаётся контакт + сделка по режиму."""
    crm = AsyncMock()
    crm.get_lead = AsyncMock(return_value=None)  # лид удалён в B24
    crm.find_reusable_lead_by_phone = AsyncMock(return_value=None)
    crm.create_contact = AsyncMock(return_value=ContactInfo(id=70))
    crm.create_deal = AsyncMock(return_value=DealInfo(id=71))
    crm.add_timeline_comment = AsyncMock(return_value=786)
    sync = _make_sync(crm)
    result = await sync.process_inbound(
        sender_name="Гость",
        sender_phone="+7999",
        message_text="Тест сделки",
        assigned_b24_user_id=1,
        existing_entity_id=55,
        existing_entity_type="lead",  # протухшая lead-привязка диалога
        crm_mode="deal",
    )

    crm.create_contact.assert_awaited_once()
    crm.create_deal.assert_awaited_once()
    crm.create_lead.assert_not_awaited()
    assert result.crm_entity_type == "deal"
    assert result.crm_entity_id == 71
    assert result.contact_id == 70
    assert result.is_new is True


# --- Маппинг источников (app_settings.source_map) ------------------------- #


@pytest.mark.asyncio
async def test_source_map_overrides_channel_default():
    """Панель подменила источник: карточка получает его, а не дефолт канала."""
    crm = _crm_new_lead()
    sync = _make_sync(crm)
    await sync.process_inbound(
        sender_name="Иван",
        sender_phone="+7999",
        message_text="Привет",
        assigned_b24_user_id=1,
        crm_mode="lead",
        source_map={Messenger.tg: "CALL"},
    )
    assert crm.create_lead.call_args.kwargs["source"] == "CALL"


@pytest.mark.asyncio
async def test_source_map_empty_string_disables_source():
    crm = _crm_new_lead()
    sync = _make_sync(crm)
    await sync.process_inbound(
        sender_name="Иван",
        sender_phone="+7999",
        message_text="Привет",
        assigned_b24_user_id=1,
        crm_mode="lead",
        source_map={Messenger.tg: ""},
    )
    assert crm.create_lead.call_args.kwargs["source"] == ""


@pytest.mark.asyncio
async def test_source_map_other_channel_untouched():
    """Канала нет в мапе (или мапы нет) — дефолт профиля канала."""
    crm = _crm_new_lead()
    sync = _make_sync(crm)
    await sync.process_inbound(
        sender_name="Иван",
        sender_phone="+7999",
        message_text="Привет",
        assigned_b24_user_id=1,
        crm_mode="lead",
        source_map={Messenger.max: "WEB"},
    )
    assert crm.create_lead.call_args.kwargs["source"] == "telegram"
