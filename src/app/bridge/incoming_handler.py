"""IncomingHandler — связка входящего TG-сообщения с CRM и нашей БД."""

import logging
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.b24.sync import Bitrix24Sync
from app.bridge.session_manager import SessionManager
from app.messaging.types import IncomingMessage
from app.models import (
    Contact,
    Dialog,
    Message,
    MessageDirection,
    MessageStatus,
    Messenger,
)

logger = logging.getLogger(__name__)


class IncomingHandler:
    """Связка: IncomingMessage (из TG) → Bitrix24Sync + сохранение в БД."""

    def __init__(
        self,
        session_mgr: SessionManager,
        b24sync: Bitrix24Sync,
        db_session_factory: Callable[[], AsyncSession],
    ):
        self._session_mgr = session_mgr
        self._b24sync = b24sync
        self._db_factory = db_session_factory

    async def handle(self, msg: IncomingMessage, *, account) -> None:
        assigned_b24_user_id = account.manager.b24_user_id

        # 1. Bitrix24Sync: матчинг → создание → timeline → notify
        result = None
        try:
            result = await self._b24sync.process_inbound(
                sender_name=msg.sender_name or "",
                sender_phone=msg.sender_phone or "",
                message_text=msg.text or "",
                assigned_b24_user_id=assigned_b24_user_id,
            )
        except Exception:
            logger.exception(
                "Bitrix24Sync failed for msg from tg_id=%s", msg.sender_tg_id
            )

        # 2. Сохранение в нашей БД (даже если CRM-синхронизация упала)
        # ВАЖНО: диалог привязываем к Manager.id (ответственный менеджер), НЕ к
        # account.id — API фильтрует диалоги по manager.id (Dialog.assigned_user_id).
        await self._persist(msg, result, manager_id=account.manager_id)

    async def _persist(self, msg: IncomingMessage, sync_result, *, manager_id: int) -> None:
        async with self._db_factory() as session:
            # Контакт: upsert по tg_user_id
            existing = await session.execute(
                select(Contact).where(Contact.tg_user_id == msg.sender_tg_id)
            )
            contact = existing.scalar_one_or_none()
            if contact is None:
                contact = Contact(
                    tg_user_id=msg.sender_tg_id,
                    phone=msg.sender_phone,
                    username=msg.sender_username,
                    name=msg.sender_name,
                )
                session.add(contact)
                await session.flush()  # получаем contact.id
            else:
                # Обновляем метаданные, если пришли новые.
                if msg.sender_phone:
                    contact.phone = msg.sender_phone
                if msg.sender_username:
                    contact.username = msg.sender_username
                if msg.sender_name:
                    contact.name = msg.sender_name

            if sync_result and sync_result.contact_id:
                contact.crm_contact_id = sync_result.contact_id

            # Диалог: upsert по external_chat_id
            existing_dialog = await session.execute(
                select(Dialog).where(Dialog.external_chat_id == msg.external_chat_id)
            )
            dialog = existing_dialog.scalar_one_or_none()
            if dialog is None:
                dialog = Dialog(
                    contact_id=contact.id,
                    messenger=Messenger.tg,
                    external_chat_id=msg.external_chat_id,
                    assigned_user_id=manager_id,
                )
                session.add(dialog)
                await session.flush()
            if sync_result and sync_result.deal_id:
                dialog.crm_deal_id = sync_result.deal_id
                dialog.crm_entity_type = "deal"

            # Идемпотентность: MTProto может дублировать доставку (реботы,
            # рестарт bridge). Пропускаем уже сохранённое сообщение по
            # (dialog, tg_message_id), иначе создадим дубль и повторно
            # пошлём timeline-комментарий и уведомление менеджеру.
            if msg.external_message_id is not None:
                existing_msg = await session.execute(
                    select(Message).where(
                        Message.dialog_id == dialog.id,
                        Message.tg_message_id == msg.external_message_id,
                    )
                )
                if existing_msg.scalar_one_or_none() is not None:
                    await session.commit()
                    return

            message = Message(
                dialog_id=dialog.id,
                direction=MessageDirection.inbound,
                tg_message_id=msg.external_message_id,
                text=msg.text,
                status=MessageStatus.delivered,
                timeline_comment_id=(
                    sync_result.timeline_comment_id if sync_result else None
                ),
            )
            session.add(message)
            await session.flush()
            # Обновляем «последнее сообщение» для сортировки списка диалогов.
            dialog.last_msg_at = message.created_at
            await session.commit()
