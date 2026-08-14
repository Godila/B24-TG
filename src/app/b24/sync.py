"""Bitrix24Sync: оркестрация входящего сообщения → CRM."""

import logging

from app.b24.crm import CrmService
from app.b24.im import ImService
from app.b24.token_manager import TokenManager

logger = logging.getLogger(__name__)

# Префикс timeline-комментария исходящего сообщения (spec §8.2 шаг 6).
OUTBOUND_COMMENT_PREFIX = "💬 Исходящее (менеджер): "


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
    ) -> SyncResult | None:
        token = await self._token_mgr.get_token()
        if token is None:
            logger.error("No B24 token — integration not installed")
            return None
        auth = token.access_token

        # 1. Матчинг по номеру
        contact = await self._crm.find_contact_by_phone(auth, sender_phone)
        is_new = contact is None

        # 2. Новый → создаём Контакт + Сделку. Существующий → ищем его
        #    ОТКРЫТУЮ сделку (идемпотентность: раньше deal_id навсегда
        #    оставался None и все комментарии падали в карточку контакта).
        if is_new:
            name = sender_name or sender_phone
            contact = await self._crm.create_contact(
                auth,
                name=name,
                phone=sender_phone,
                assigned_by_id=assigned_b24_user_id,
            )
            deal = await self._crm.create_deal(
                auth,
                title=f"TG: {name}",
                contact_id=contact.id,
                assigned_by_id=assigned_b24_user_id,
            )
            deal_id = deal.id
        else:
            deal = await self._crm.find_open_deal_for_contact(auth, contact.id)
            # Нет открытых сделок — не создаём (клиент уже в работе):
            # комментарий уйдёт в карточку контакта, deal_id=None.
            deal_id = deal.id if deal is not None else None

        # 3. Запись в timeline. Если есть сделка — пишем в сделку,
        #    иначе — в карточку контакта (история диалога сохраняется).
        if deal_id is not None:
            comment_id = await self._crm.add_timeline_comment(
                auth, entity_type="deal", entity_id=deal_id, comment=message_text,
            )
        else:
            comment_id = await self._crm.add_timeline_comment(
                auth,
                entity_type="contact",
                entity_id=contact.id,
                comment=message_text,
            )

        # 4. Уведомление ответственному — ТОЛЬКО первому сообщению нового
        #    клиента (is_new): раньше слалось на каждое входящее (спам
        #    менеджеру + расход REST-квоты).
        if is_new:
            await self._im.notify_manager(
                auth,
                user_id=assigned_b24_user_id,
                message=(
                    f"💬 Новое сообщение в Telegram от "
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
    ) -> int | None:
        """Timeline-комментарий для исходящего сообщения (deal или contact-карточка).

        Spec §8.2 шаг 6: исходящее сообщение менеджера тоже попадает в
        историю CRM. Приоритет привязки: сделка диалога → карточка контакта;
        нет ни той, ни другой — писать некуда, возвращаем None.
        Возвращает ID timeline-комментария или None.
        """
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
