"""Bitrix24Sync: оркестрация входящего сообщения → CRM."""

import logging
from typing import NamedTuple

from app.b24.channels import B24ChannelProfile, channel_profile
from app.b24.crm import CrmService
from app.b24.im import ImService
from app.b24.token_manager import TokenManager
from app.models import Messenger

logger = logging.getLogger(__name__)

# Префикс timeline-комментария исходящего сообщения (spec §8.2 шаг 6).
OUTBOUND_COMMENT_PREFIX = "💬 Исходящее (менеджер): "

#: Режимы дублирования переписки в таймлайн CRM (app_settings.timeline_mode):
#:  all   — комментарий на каждое входящее и исходящее (полный аудит, шумно);
#:  first — только первое сообщение нового диалога («Диалог открыт»);
#:  none  — в таймлайн не пишем ничего (переписка живёт в виджете).
TIMELINE_MODES = ("all", "first", "none")
TIMELINE_MODE_DEFAULT = "first"

#: Какие CRM-карточки заводить новому клиенту (app_settings.crm_mode):
#:  deal — контакт + сделка (классическая схема);
#:  lead — только лид; контакт/сделку создаст конвертация менеджером.
#: Режим применяется к НОВЫМ клиентам: тип живой привязки диалога
#: авторитетнее (переключение режима не перекладывает существующие диалоги).
CRM_MODES = ("deal", "lead")
CRM_MODE_DEFAULT = "deal"


class SyncResult:
    """Результат обработки входящего сообщения.

    crm_entity_type/crm_entity_id — сущность диалога ('deal'|'lead'; id
    живёт в колонке dialogs.crm_deal_id). contact_id — CRM-контакт (в
    lead-режиме обычно None, появляется после конвертации лида).
    """

    __slots__ = ("contact_id", "crm_entity_id", "crm_entity_type", "is_new", "timeline_comment_id")

    def __init__(
        self,
        crm_entity_type: str | None,
        crm_entity_id: int | None,
        contact_id: int | None,
        is_new: bool,
        timeline_comment_id: int | None = None,
    ):
        self.crm_entity_type = crm_entity_type
        self.crm_entity_id = crm_entity_id
        self.contact_id = contact_id
        self.is_new = is_new
        self.timeline_comment_id = timeline_comment_id


class _Entities(NamedTuple):
    """Разрешённая CRM-сущность комментария + контакт + «новый клиент».

    entity_type/entity_id=None → сущности нет, комментарий уйдёт в карточку
    контакта (нет и контакта — писать некуда).
    """

    entity_type: str | None
    entity_id: int | None
    contact_id: int | None
    is_new: bool


class Bitrix24Sync:
    """Оркестрация: входящее сообщение → CRM.

    Матчинг по номеру → создание/привязка сущностей → timeline → уведомление.
    """

    def __init__(self, token_mgr: TokenManager, crm: CrmService, im: ImService):
        self._token_mgr = token_mgr
        self._crm = crm
        self._im = im

    async def process_inbound(
        self,
        sender_name: str,
        sender_phone: str,
        message_text: str,
        assigned_b24_user_id: int | None,
        *,
        messenger: Messenger = Messenger.tg,
        notify_user_ids: list[int] | None = None,
        existing_contact_id: int | None = None,
        existing_entity_id: int | None = None,
        existing_entity_type: str | None = None,
        crm_mode: str = CRM_MODE_DEFAULT,
        # Дефолт — TIMELINE_MODE_DEFAULT (дефолт приложения), а не "all":
        # вызов без явного режима не должен молча включать самый шумный.
        timeline_mode: str = TIMELINE_MODE_DEFAULT,
        sender_first_name: str | None = None,
        sender_last_name: str | None = None,
        sender_username: str | None = None,
        files: list[tuple[str, str]] | None = None,
    ) -> SyncResult | None:
        """Входящее сообщение → CRM.

        ``messenger`` параметризует тексты (уведомление, источник SOURCE_ID)
        по каналу. ``assigned_b24_user_id`` — ответственный CRM (None у
        общего номера: карточка создаётся без ответственного, B24 применит
        свои правила очереди). ``notify_user_ids`` — адресаты уведомления о
        новом клиенте (дефолт: ответственный, у общих линий — все активные
        участники, собирает crm_sync_repo.collect).
        ``existing_contact_id``/``existing_entity_id``+``existing_entity_type``
        — уже известные CRM-связи диалога/контакта: живая привязка
        приоритетнее поиска по телефону (findbyComm по пустому телефону у
        MAX-клиентов плодил бы дубли). ``crm_mode`` (app_settings) решает,
        какую карточку заводить НОВОМУ клиенту — 'deal' (контакт+сделка)
        или 'lead' (только лид); тип живой привязки авторитетнее режима.
        ``timeline_mode`` (app_settings): all/first/none — что писать в
        таймлайн (уведомление менеджеру режимом не трогается).
        ``sender_first_name``/``sender_last_name``/``sender_username`` —
        дополнительные данные канала: split-имя в NAME/LAST_NAME, @username
        (TG) — в мульти-поле IM.
        ``files`` — [(имя, base64)] вложений для FILES комментария
        (app_settings.media_to_timeline): в карточку CRM попадает сам
        файл, а не только метка «[фото]».
        """
        token = await self._token_mgr.get_token()
        if token is None:
            logger.error("No B24 token — integration not installed")
            return None
        auth = token.access_token
        profile = channel_profile(messenger)
        name = sender_name or sender_phone or "Без имени"

        # 1. Разрешение CRM-сущности: тип живой привязки диалога
        #    авторитетнее режима (см. CRM_MODES).
        kind = existing_entity_type or crm_mode
        if kind == "lead":
            entity = await self._resolve_lead_entity(
                auth,
                name=name,
                phone=sender_phone,
                assigned=assigned_b24_user_id,
                profile=profile,
                existing_entity_id=existing_entity_id,
                first_name=sender_first_name,
                last_name=sender_last_name,
                username=sender_username,
            )
        else:
            entity = await self._resolve_deal_entity(
                auth,
                name=name,
                phone=sender_phone,
                assigned=assigned_b24_user_id,
                profile=profile,
                existing_contact_id=existing_contact_id,
                existing_entity_id=existing_entity_id,
                first_name=sender_first_name,
                last_name=sender_last_name,
                username=sender_username,
            )

        # 2. Запись в timeline по режиму администратора (app_settings).
        #    first: только сообщение, открывшее диалог (is_new); none: ничего.
        comment_id: int | None = None
        write_comment = timeline_mode == "all" or (timeline_mode == "first" and entity.is_new)
        if write_comment:
            comment_text = message_text
            if timeline_mode == "first":
                comment_text = f"💬 Диалог открыт ({profile.notify_label}): {message_text}"
            if entity.entity_id is not None:
                comment_id = await self._crm.add_timeline_comment(
                    auth,
                    entity_type=entity.entity_type,
                    entity_id=entity.entity_id,
                    comment=comment_text,
                    files=files,
                )
            elif entity.contact_id is not None:
                comment_id = await self._crm.add_timeline_comment(
                    auth,
                    entity_type="contact",
                    entity_id=entity.contact_id,
                    comment=comment_text,
                    files=files,
                )

        # 3. Уведомление — ТОЛЬКО первому сообщению нового клиента (is_new):
        #    раньше слалось на каждое входящее (спам + расход REST-квоты).
        #    Адресаты — ответственный, а у общего номера все активные
        #    участники линии (список от collect).
        if entity.is_new:
            recipients = notify_user_ids if notify_user_ids is not None else (
                [assigned_b24_user_id] if assigned_b24_user_id is not None else []
            )
            for user_id in recipients:
                await self._im.notify_manager(
                    auth,
                    user_id=user_id,
                    message=(
                        f"💬 Новое сообщение в {profile.notify_label} от "
                        f"{sender_name or sender_phone}:\n{message_text}"
                    ),
                )

        return SyncResult(
            crm_entity_type=entity.entity_type,
            crm_entity_id=entity.entity_id,
            contact_id=entity.contact_id,
            is_new=entity.is_new,
            timeline_comment_id=comment_id,
        )

    async def _resolve_deal_entity(
        self,
        auth: str,
        *,
        name: str,
        phone: str,
        assigned: int | None,
        profile: B24ChannelProfile,
        existing_contact_id: int | None,
        existing_entity_id: int | None,
        first_name: str | None,
        last_name: str | None,
        username: str | None,
    ) -> _Entities:
        """Режим 'deal': матчинг контакта → контакт + сделка."""
        # Матчинг: известная CRM-связка приоритетнее поиска по телефону.
        if existing_contact_id is not None:
            contact = await self._crm.get_contact(auth, existing_contact_id)
            if contact is None:
                # Связка протухла (контакт удалён в B24) — ищем заново.
                contact = await self._crm.find_contact_by_phone(auth, phone)
        else:
            contact = await self._crm.find_contact_by_phone(auth, phone)

        # Новый → создаём Контакт + Сделку. Существующий → его ОТКРЫТУЮ
        # сделку (идемпотентность: раньше deal_id навсегда оставался None
        # и все комментарии падали в карточку контакта).
        if contact is None:
            contact = await self._crm.create_contact(
                auth,
                name=name,
                phone=phone,
                assigned_by_id=assigned,
                source=profile.source_id,
                first_name=first_name,
                last_name=last_name,
                username=username,
            )
            deal = await self._crm.create_deal(
                auth,
                title=name,
                contact_id=contact.id,
                assigned_by_id=assigned,
                source=profile.source_id,
            )
            return _Entities("deal", deal.id, contact.id, True)
        if existing_entity_id is not None:
            return _Entities("deal", existing_entity_id, contact.id, False)
        deal = await self._crm.find_open_deal_for_contact(auth, contact.id)
        # Нет открытых сделок — не создаём (клиент уже в работе):
        # комментарий уйдёт в карточку контакта, сущности нет.
        if deal is None:
            return _Entities(None, None, contact.id, False)
        return _Entities("deal", deal.id, contact.id, False)

    async def _resolve_lead_entity(
        self,
        auth: str,
        *,
        name: str,
        phone: str,
        assigned: int | None,
        profile: B24ChannelProfile,
        existing_entity_id: int | None,
        first_name: str | None,
        last_name: str | None,
        username: str | None,
    ) -> _Entities:
        """Режим 'lead': только лид, без контакта (создаст конвертация)."""
        # A. Живая привязка диалога к лиду.
        if existing_entity_id is not None:
            lead = await self._crm.get_lead(auth, existing_entity_id)
            if lead is not None and lead.status_id != "CONVERTED":
                return _Entities("lead", lead.id, None, False)
            if lead is not None:
                # Сконвертирован в B24 — ребинд: контакт конвертации
                # (фолбэк — поиск по телефону) → его ОТКРЫТАЯ сделка;
                # apply_inbound_result перепривяжет диалог, дальше
                # комментарии и виджет живут в карточке сделки. Открытой
                # сделки нет — остаёмся на лиде (комментарий в карточку лида).
                contact_id = lead.contact_id
                if contact_id is None and phone:
                    found = await self._crm.find_contact_by_phone(auth, phone)
                    contact_id = found.id if found else None
                if contact_id is not None:
                    deal = await self._crm.find_open_deal_for_contact(auth, contact_id)
                    if deal is not None:
                        return _Entities("deal", deal.id, contact_id, False)
                return _Entities("lead", lead.id, contact_id, False)
            # Лид удалён в B24 — связка протухла, ищем заново ниже.
        # B. Матчинг по телефону: пригодный лид (не CONVERTED/JUNK).
        lead = await self._crm.find_reusable_lead_by_phone(auth, phone) if phone else None
        if lead is not None:
            return _Entities("lead", lead.id, None, False)
        # C. Новый клиент — классический crm.lead.add (повторное обращение
        #    без живого лида = новый лид, штатный паттерн работы с лидами).
        lead = await self._crm.create_lead(
            auth,
            title=name,
            phone=phone,
            assigned_by_id=assigned,
            source=profile.source_id,
            first_name=first_name,
            last_name=last_name,
            username=username,
        )
        return _Entities("lead", lead.id, None, True)

    async def process_outbound(
        self,
        dialog_entity_id: int | None,
        dialog_entity_type: str | None,
        contact_id: int | None,
        text: str,
        *,
        timeline_mode: str = TIMELINE_MODE_DEFAULT,
        files: list[tuple[str, str]] | None = None,
    ) -> int | None:
        """Timeline-комментарий для исходящего сообщения (deal/lead или
        карточка контакта).

        Spec §8.2 шаг 6: исходящее сообщение менеджера тоже попадает в
        историю CRM (в режиме ``all``). ``first``/``none`` исходящие в
        таймлайн не дублируют. Приоритет привязки: сущность диалога
        ('deal'|'lead') → карточка контакта; нет ни той, ни другой —
        писать некуда, возвращаем None. ``files`` — вложения для FILES
        комментария (как у process_inbound). Возвращает ID
        timeline-комментария или None.
        """
        if timeline_mode != "all":
            return None
        token = await self._token_mgr.get_token()
        if token is None:
            logger.error("No B24 token — integration not installed")
            return None
        auth = token.access_token

        comment = OUTBOUND_COMMENT_PREFIX + text
        if dialog_entity_id is not None:
            entity_type = dialog_entity_type or "deal"
            return await self._crm.add_timeline_comment(
                auth,
                entity_type=entity_type,
                entity_id=dialog_entity_id,
                comment=comment,
                files=files,
            )
        if contact_id is not None:
            return await self._crm.add_timeline_comment(
                auth,
                entity_type="contact",
                entity_id=contact_id,
                comment=comment,
                files=files,
            )
        return None
