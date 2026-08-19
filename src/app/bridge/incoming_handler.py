"""IncomingHandler — сохранение входящего сообщения + постановка CRM-задачи.

План 006: сообщение СНАЧАЛА сохраняется в нашей БД (без CRM-полей), затем в
очередь ``crm_sync`` ставится задача kind=inbound — CRM-вызовы делает
CrmSyncWorker с ретраями. Раньше Bitrix24Sync звался прямо в пути события и
любой сбой B24 (rate-limit free-портала, сеть) молча терял контакт/сделку/
timeline-комментарий навсегда.

Канал-нейтрально: messenger диалога/контакта берётся из IncomingMessage,
идентичность контакта — пара (messenger, external_user_id).

Device-outbound (direction=outbound): сообщение менеджера, отправленное
с устройства (не из виджета) — та же труба, но Message(out, sent) без
outbox и задача kind=outbound. Только существующие диалоги (решение
владельца продукта): нового клиента с устройства не заводим.
"""

import logging
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.bridge.crm_sync_worker import CrmSyncEnqueue
from app.messaging.types import IncomingMessage
from app.models import (
    KIND_INBOUND,
    KIND_OUTBOUND,
    AccountMember,
    Attachment,
    AttachmentType,
    Contact,
    Dialog,
    Manager,
    Message,
    MessageDirection,
    MessageStatus,
)

logger = logging.getLogger(__name__)


async def line_assignee(session: AsyncSession, account) -> int | None:
    """Интерим-маршрутизация линии: ровно один АКТИВНЫЙ участник — он и
    ответственный (личный номер = прежнее поведение 1:1); 0 или ≥2 —
    «общий номер», диалог без ответственного. Распределение входящих
    (round-robin/клейм/лиды) — отдельная будущая фича, встанет сюда."""
    member_ids = (
        (
            await session.execute(
                select(AccountMember.manager_id)
                .join(Manager, Manager.id == AccountMember.manager_id)
                .where(
                    AccountMember.account_id == account.id,
                    Manager.is_active.is_(True),
                )
                .order_by(AccountMember.id)
            )
        )
        .scalars()
        .all()
    )
    return member_ids[0] if len(member_ids) == 1 else None


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
        # Диалог принадлежит ЛИНИИ (account.id); ответственный — интерим-
        # правило line_assignee (единый активный участник личной линии или
        # NULL у общего номера). Состав линии — источник видимости (API).
        message_id = await self._persist(msg, account=account)
        if message_id is None:
            return  # дубль доставки или device-outbound без диалога

        # 2. CRM-синхронизация — через очередь (воркер с ретраями).
        kind = KIND_OUTBOUND if msg.direction == MessageDirection.outbound else KIND_INBOUND
        try:
            await self._crm_sync_enqueue(kind=kind, message_id=message_id)
        except Exception:
            logger.exception(
                "crm_sync enqueue failed for message_id=%s (external msg %s)",
                message_id,
                msg.external_message_id,
            )

    async def _persist(self, msg: IncomingMessage, *, account) -> int | None:
        """Сохранить сообщение; вернуть его id или None для дубля доставки."""
        if msg.direction == MessageDirection.outbound:
            return await self._persist_outbound(msg, account=account)
        async with self._db_factory() as session:
            contact = await self._upsert_contact(session, msg)
            assignee = await line_assignee(session, account)

            dialog = await self._find_dialog(session, msg, account)
            if dialog is None:
                dialog = Dialog(
                    contact_id=contact.id,
                    messenger=msg.messenger,
                    external_chat_id=msg.external_chat_id,
                    account_id=account.id,
                    assigned_user_id=assignee,
                )
                session.add(dialog)
                try:
                    await session.flush()
                except IntegrityError:
                    # Гонка: параллельная задача уже вставила диалог этой
                    # линии. Rollback откатывает и контактную часть txn —
                    # получаем контакт заново, затем берём существующий диалог.
                    await session.rollback()
                    contact = await self._upsert_contact(session, msg)
                    dialog = await self._find_dialog(session, msg, account)
                    assert dialog is not None  # гонка вставки — диалог уже есть

            # Идемпотентность: канал может дублировать доставку (реботы,
            # рестарт bridge). Пропускаем уже сохранённое сообщение по
            # (dialog, external_message_id), иначе создадим дубль и повторно
            # поставим CRM-задачу.
            if msg.external_message_id is not None and await self._message_exists(
                session, dialog.id, msg.external_message_id
            ):
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
            # Медиа: файл уже скачан провайдером на общий том — здесь
            # только метаданные. Append ДО flush: у transient-объекта
            # коллекция инициализируется без lazy-load (после flush это
            # роняло бы MissingGreenlet), каскад сам поставит FK.
            if msg.media is not None:
                message.attachments.append(self._attachment(msg))
            await session.flush()
            # Обновляем «последнее сообщение» для сортировки списка диалогов.
            dialog.last_msg_at = message.created_at
            message_id = message.id
            await session.commit()
            return message_id

    async def _persist_outbound(self, msg: IncomingMessage, *, account) -> int | None:
        """Device-outbound: написано с телефона номера, уже доставлено.

        Без outbox (отправка прошла мимо нас) → сразу status=sent и
        sent_at=время канала. Контакт не апсертим — он уже связан с
        диалогом; sender_* сообщения описывают самого менеджера и здесь
        не читаются.
        """
        async with self._db_factory() as session:
            dialog = await self._find_dialog(session, msg, account)
            if dialog is None:
                logger.info(
                    "device-outbound скип: диалог не найден (messenger=%s chat=%s account=%s)",
                    msg.messenger.value,
                    msg.external_chat_id,
                    account.id,
                )
                return None
            if msg.external_message_id is not None and await self._message_exists(
                session, dialog.id, msg.external_message_id
            ):
                # Дубль доставки или эхо виджетной отправки: mark_sent уже
                # записал этот external_message_id в outbound-строку, а дедуп
                # direction-агностичен.
                return None
            # Автор — единственный участник личной линии; у общего номера
            # с телефона писал неизвестный нам человек (author=None).
            assignee = await line_assignee(session, account)
            author_b24_user_id = (
                await session.scalar(
                    select(Manager.b24_user_id).where(Manager.id == assignee)
                )
                if assignee is not None
                else None
            )
            message = Message(
                dialog_id=dialog.id,
                direction=MessageDirection.outbound,
                external_message_id=msg.external_message_id,
                text=msg.text,
                status=MessageStatus.sent,
                author_user_id=author_b24_user_id,
                sent_at=msg.timestamp,
            )
            session.add(message)
            if msg.media is not None:
                message.attachments.append(self._attachment(msg))
            await session.flush()
            dialog.last_msg_at = message.created_at
            message_id = message.id
            await session.commit()
            return message_id

    @staticmethod
    def _attachment(msg: IncomingMessage) -> Attachment:
        """Attachment-строка по скачанному провайдером медиа (метаданные).

        Вызывать только при ``msg.media is not None`` и appending ДО flush
        (см. комментарий в _persist).
        """
        media = msg.media
        assert media is not None  # narrowing для типизации; гард — на вызывающем
        return Attachment(
            type=AttachmentType(msg.content_type.value),
            file_path=media.path,
            mime_type=media.mime_type,
            size=media.size,
            file_name=media.file_name,
        )

    async def _find_dialog(
        self, session: AsyncSession, msg: IncomingMessage, account
    ) -> Dialog | None:
        """Диалог линии аккаунта по (messenger, external_chat_id, account_id).

        Линия — источник идентичности диалога (один клиент на линии = один
        диалог и его история, независимо от смены/сброса ответственного).
        Мультиканал: id-пространства TG и MAX независимы. Legacy-дубли
        (chat, manager) до миграции линий засеяны уникальным account_id —
        берём старейший, чтобы не упасть MultipleResultsFound.
        """
        return (
            await session.execute(
                select(Dialog)
                .where(
                    Dialog.messenger == msg.messenger,
                    Dialog.external_chat_id == msg.external_chat_id,
                    Dialog.account_id == account.id,
                )
                .order_by(Dialog.id)
                .limit(1)
            )
        ).scalar_one_or_none()

    @staticmethod
    async def _message_exists(
        session: AsyncSession, dialog_id: int, external_message_id: str
    ) -> bool:
        """Есть ли уже сообщение с этим внешним id в диалоге.

        Без фильтра direction: эхо виджетной отправки приходит self-пушем с
        тем же external_message_id, что записал mark_sent в outbound-строку.
        """
        existing = await session.execute(
            select(Message.id)
            .where(
                Message.dialog_id == dialog_id,
                Message.external_message_id == external_message_id,
            )
            .limit(1)
        )
        return existing.scalar_one_or_none() is not None

    async def _upsert_contact(self, session: AsyncSession, msg: IncomingMessage) -> Contact:
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
                first_name=msg.sender_first_name,
                last_name=msg.sender_last_name,
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
            if msg.sender_first_name:
                contact.first_name = msg.sender_first_name
            if msg.sender_last_name:
                contact.last_name = msg.sender_last_name
        return contact
