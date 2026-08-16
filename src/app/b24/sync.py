"""Bitrix24Sync: оркестрация входящего сообщения → CRM."""

import logging

from app.b24.channels import channel_profile
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


class SyncResult:
    """Результат обработки входящего сообщения."""

    __slots__ = ("contact_id", "deal_id", "is_new", "timeline_comment_id")

    def __init__(
        self,
        contact_id: int,
        deal_id: int | None,
        is_new: bool,
        timeline_comment_id: int | None = None,
    ):
        self.contact_id = contact_id
        self.deal_id = deal_id
        self.is_new = is_new
        self.timeline_comment_id = timeline_comment_id


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
        assigned_b24_user_id: int,
        *,
        messenger: Messenger = Messenger.tg,
        existing_contact_id: int | None = None,
        existing_deal_id: int | None = None,
        timeline_mode: str = "all",
    ) -> SyncResult | None:
        """Входящее сообщение → CRM.

        ``messenger`` параметризует тексты (префикс сделки, уведомление,
        источник) по каналу. ``existing_contact_id``/``existing_deal_id`` —
        уже известные CRM-связи диалога/контакта: если контакт связан,
        пропускаем поиск по телефону (findbyComm по пустому телефону у
        MAX-клиентов плодил бы дубли), если сделка связана — не ищем открытую.
        ``timeline_mode`` (app_settings): all/first/none — что писать в
        таймлайн (уведомление менеджеру режимом не трогается).
        """
        token = await self._token_mgr.get_token()
        if token is None:
            logger.error("No B24 token — integration not installed")
            return None
        auth = token.access_token
        profile = channel_profile(messenger)

        # 1. Матчинг: известная CRM-связка приоритетнее поиска по телефону.
        if existing_contact_id is not None:
            contact = await self._crm.get_contact(auth, existing_contact_id)
            if contact is None:
                # Связка протухла (контакт удалён в B24) — ищем заново.
                contact = await self._crm.find_contact_by_phone(auth, sender_phone)
        else:
            contact = await self._crm.find_contact_by_phone(auth, sender_phone)
        is_new = contact is None

        # 2. Новый → создаём Контакт + Сделку. Существующий → его ОТКРЫТУЮ
        #    сделку (идемпотентность: раньше deal_id навсегда оставался None
        #    и все комментарии падали в карточку контакта).
        if is_new:
            name = sender_name or sender_phone or "Без имени"
            contact = await self._crm.create_contact(
                auth,
                name=name,
                phone=sender_phone,
                assigned_by_id=assigned_b24_user_id,
                source=profile.source_id,
            )
            deal = await self._crm.create_deal(
                auth,
                title=f"{profile.deal_prefix}{name}",
                contact_id=contact.id,
                assigned_by_id=assigned_b24_user_id,
            )
            deal_id = deal.id
        elif existing_deal_id is not None:
            deal_id = existing_deal_id
        else:
            deal = await self._crm.find_open_deal_for_contact(auth, contact.id)
            # Нет открытых сделок — не создаём (клиент уже в работе):
            # комментарий уйдёт в карточку контакта, deal_id=None.
            deal_id = deal.id if deal is not None else None

        # 3. Запись в timeline по режиму администратора (app_settings).
        #    first: только сообщение, открывшее диалог (is_new); none: ничего.
        comment_id: int | None = None
        write_comment = timeline_mode == "all" or (
            timeline_mode == "first" and is_new
        )
        if write_comment:
            comment_text = message_text
            if timeline_mode == "first":
                comment_text = f"💬 Диалог открыт ({profile.notify_label}): {message_text}"
            if deal_id is not None:
                comment_id = await self._crm.add_timeline_comment(
                    auth, entity_type="deal", entity_id=deal_id, comment=comment_text,
                )
            else:
                comment_id = await self._crm.add_timeline_comment(
                    auth,
                    entity_type="contact",
                    entity_id=contact.id,
                    comment=comment_text,
                )

        # 4. Уведомление ответственному — ТОЛЬКО первому сообщению нового
        #    клиента (is_new): раньше слалось на каждое входящее (спам
        #    менеджеру + расход REST-квоты).
        if is_new:
            await self._im.notify_manager(
                auth,
                user_id=assigned_b24_user_id,
                message=(
                    f"💬 Новое сообщение в {profile.notify_label} от "
                    f"{sender_name or sender_phone}:\n{message_text}"
                ),
            )

        return SyncResult(
            contact_id=contact.id,
            deal_id=deal_id,
            is_new=is_new,
            timeline_comment_id=comment_id,
        )

    async def process_outbound(
        self,
        dialog_deal_id: int | None,
        dialog_entity_type: str | None,
        contact_id: int | None,
        text: str,
        *,
        timeline_mode: str = "all",
    ) -> int | None:
        """Timeline-комментарий для исходящего сообщения (deal или contact-карточка).

        Spec §8.2 шаг 6: исходящее сообщение менеджера тоже попадает в
        историю CRM (в режиме ``all``). ``first``/``none`` исходящие в
        таймлайн не дублируют. Приоритет привязки: сделка диалога → карточка
        контакта; нет ни той, ни другой — писать некуда, возвращаем None.
        Возвращает ID timeline-комментария или None.
        """
        if timeline_mode != "all":
            return None
        token = await self._token_mgr.get_token()
        if token is None:
            logger.error("No B24 token — integration not installed")
            return None
        auth = token.access_token

        comment = OUTBOUND_COMMENT_PREFIX + text
        if dialog_deal_id is not None:
            entity_type = dialog_entity_type or "deal"
            return await self._crm.add_timeline_comment(
                auth, entity_type=entity_type, entity_id=dialog_deal_id, comment=comment,
            )
        if contact_id is not None:
            return await self._crm.add_timeline_comment(
                auth, entity_type="contact", entity_id=contact_id, comment=comment,
            )
        return None
