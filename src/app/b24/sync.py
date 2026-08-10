"""Bitrix24Sync: оркестрация входящего сообщения → CRM."""

import logging

from app.b24.crm import CrmService
from app.b24.im import ImService
from app.b24.token_manager import TokenManager

logger = logging.getLogger(__name__)


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

        # 2. Новый → создаём Контакт + Сделку
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
            # Существующий контакт: новую сделку не создаём.
            deal_id = None

        # 3. Запись в timeline. Если есть сделка — пишем в сделку,
        # иначе — в карточку контакта (история диалога сохраняется).
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

        # 4. Уведомление ответственному менеджеру
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
