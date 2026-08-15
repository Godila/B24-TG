"""IncomingHandler — сохранение входящего сообщения + постановка CRM-задачи.

План 006: сообщение СНАЧАЛА сохраняется в нашей БД (без CRM-полей), затем в
очередь ``crm_sync`` ставится задача kind=inbound — CRM-вызовы делает
CrmSyncWorker с ретраями. Раньше Bitrix24Sync звался прямо в пути события и
любой сбой B24 (rate-limit free-портала, сеть) молча терял контакт/сделку/
timeline-комментарий навсегда.

Канал-нейтрально: messenger диалога/контакта берётся из IncomingMessage,
идентичность контакта — пара (messenger, external_user_id).
"""

import logging
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.bridge.crm_sync_worker import CrmSyncEnqueue
from app.messaging.types import IncomingMessage
from app.models import (
    Contact,
    Dialog,
    Message,
    MessageDirection,
    MessageStatus,
)

logger = logging.getLogger(__name__)


class IncomingHandler:
    """Связка: IncomingMessage (из канала) → наша БД → очередь crm_sync."""

    def __init__(
        self,
        crm_sync_enqueue: CrmSyncEnqueue,
        db_session_factory: Callable[[], AsyncSession],
    ):
        self._crm_sync_enqueue = crm_sync_enqueue
        self._db_factory = db_session_factory

    async def handle(self, msg: IncomingMessage, *, account) -> None:
        # 1. Сохранение в нашей БД — всегда, независимо от состояния CRM.
        # ВАЖНО: диалог привязываем к Manager.id (ответственный менеджер), НЕ к
        # account.id — API фильтрует диалоги по manager.id (Dialog.assigned_user_id).
        message_id = await self._persist(msg, manager_id=account.manager_id)
        if message_id is None:
            return  # дубль доставки — сообщение уже было обработано ранее

        # 2. CRM-синхронизация — через очередь (воркер с ретраями).
        try:
            await self._crm_sync_enqueue(kind="inbound", message_id=message_id)
        except Exception:
            logger.exception(
                "crm_sync enqueue failed for message_id=%s (external msg %s)",
                message_id,
                msg.external_message_id,
            )

    async def _persist(self, msg: IncomingMessage, *, manager_id: int) -> int | None:
        """Сохранить сообщение; вернуть его id или None для дубля доставки."""
        async with self._db_factory() as session:
            contact = await self._upsert_contact(session, msg)

            # Диалог: upsert по (messenger, external_chat_id, assigned_user_id)
            # — мультиаккаунт (в приватных TG-чатах chat_id == id клиента и
            # совпадает у всех менеджеров) и мультиканал (id-пространства
            # каналов независимы).
            dialog_stmt = (
                select(Dialog)
                .where(
                    Dialog.messenger == msg.messenger,
                    Dialog.external_chat_id == msg.external_chat_id,
                    Dialog.assigned_user_id == manager_id,
                )
                # Legacy-дубли (chat_id, manager) могли остаться до миграции:
                # берём старейший, чтобы не упасть MultipleResultsFound.
                .order_by(Dialog.id)
                .limit(1)
            )
            dialog = (await session.execute(dialog_stmt)).scalar_one_or_none()
            if dialog is None:
                dialog = Dialog(
                    contact_id=contact.id,
                    messenger=msg.messenger,
                    external_chat_id=msg.external_chat_id,
                    assigned_user_id=manager_id,
                )
                session.add(dialog)
                try:
                    await session.flush()
                except IntegrityError:
                    # Гонка: параллельная задача уже вставила диалог с этой
                    # тройкой. Rollback откатывает и контактную часть txn —
                    # получаем контакт заново, затем берём существующий диалог.
                    await session.rollback()
                    contact = await self._upsert_contact(session, msg)
                    dialog = (await session.execute(dialog_stmt)).scalar_one()

            # Идемпотентность: канал может дублировать доставку (реботы,
            # рестарт bridge). Пропускаем уже сохранённое сообщение по
            # (dialog, external_message_id), иначе создадим дубль и повторно
            # поставим CRM-задачу.
            if msg.external_message_id is not None:
                existing_msg = await session.execute(
                    select(Message).where(
                        Message.dialog_id == dialog.id,
                        Message.external_message_id == msg.external_message_id,
                    )
                )
                if existing_msg.scalar_one_or_none() is not None:
                    await session.commit()
                    return None

            message = Message(
                dialog_id=dialog.id,
                direction=MessageDirection.inbound,
                external_message_id=msg.external_message_id,
                text=msg.text,
                status=MessageStatus.delivered,
            )
            session.add(message)
            await session.flush()
            # Обновляем «последнее сообщение» для сортировки списка диалогов.
            dialog.last_msg_at = message.created_at
            message_id = message.id
            await session.commit()
            return message_id

    async def _upsert_contact(
        self, session: AsyncSession, msg: IncomingMessage
    ) -> Contact:
        """Контакт: upsert по (messenger, external_user_id).

        Вызывается повторно после IntegrityError-rollback по диалогу: rollback
        откатывает и вставку/правки контакта того же txn, поэтому контакт
        нужно получить заново, чтобы сессия осталась консистентной.
        """
        existing = await session.execute(
            select(Contact).where(
                Contact.messenger == msg.messenger,
                Contact.external_user_id == msg.sender_external_id,
            )
        )
        contact = existing.scalar_one_or_none()
        if contact is None:
            contact = Contact(
                messenger=msg.messenger,
                external_user_id=msg.sender_external_id,
                phone=msg.sender_phone,
                username=msg.sender_username,
                name=msg.sender_name,
            )
            session.add(contact)
            try:
                await session.flush()  # получаем contact.id
            except IntegrityError:
                # Гонка вставки контакта: форвард-таски разных менеджеров
                # параллельно обрабатывают сообщения одного нового клиента
                # (uq (messenger, external_user_id)). Без отката сообщение
                # первого менеджера терялось бы совсем.
                await session.rollback()
                contact = (
                    await session.execute(
                        select(Contact).where(
                            Contact.messenger == msg.messenger,
                            Contact.external_user_id == msg.sender_external_id,
                        )
                    )
                ).scalar_one()
                return contact
        else:
            # Обновляем метаданные, если пришли новые.
            if msg.sender_phone:
                contact.phone = msg.sender_phone
            if msg.sender_username:
                contact.username = msg.sender_username
            if msg.sender_name:
                contact.name = msg.sender_name
        return contact
