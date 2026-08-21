"""InitiationWorker — bridge-исполнитель команд «написать первым».

Поллер таблицы initiations (паттерн login_commands/outbox): подхватывает
pending-строки, резолвит peer ЖИВЫМ провайдером аккаунта (провайдеры есть
только в bridge) и в одной транзакции создаёт Contact/Dialog/Message +
enqueue в outbox (is_initiation=True → анти-бан throttler). Дальше
доставкой владеет штатный OutboxWorker.

Классификация отказов:
* resolve → None / NotImplementedError — терминально (ретраи не помогут);
* провайдера нет/офлайн — reschedule без расхода попытки, после дедлайна
  (менеджер ждёт живьём) — терминальный failed;
* сеть/протокол — backoff 30*2^attempts, max_attempts → failed.
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bridge.incoming_handler import line_assignee
from app.bridge.outbox_repo_sqlalchemy import SqlAlchemyOutboxRepository
from app.bridge.session_manager import SessionManager
from app.db import async_session
from app.messaging.resolve import ParsedDest, ResolvedPeer
from app.models import (
    Contact,
    Dialog,
    Initiation,
    InitiationStatus,
    Message,
    MessageDirection,
    MessageStatus,
    TgAccount,
    has_inbound,
)

logger = logging.getLogger(__name__)

_MAX_ATTEMPTS = 3
#: Провайдер не появился за это время (аккаунт офлайн) — менеджер не должен
#: висеть: честный failed вместо бесконечного reschedule.
_PROVIDER_DEADLINE_SEC = 120.0


class InitiationWorker:
    def __init__(
        self,
        *,
        sm: SessionManager,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        poll_interval: float = 2.0,
    ):
        self._sm = sm
        self._session_factory = session_factory or async_session
        self._poll_interval = poll_interval
        self._running = False

    async def run(self) -> None:
        self._running = True
        try:
            while self._running:
                try:
                    await self._process_once()
                except Exception:  # pragma: no cover - защитная сетка
                    logger.exception("InitiationWorker iteration failed; continuing")
                await asyncio.sleep(self._poll_interval)
        finally:
            self._running = False

    def stop(self) -> None:
        self._running = False

    async def _process_once(self) -> None:
        async with self._session_factory() as s:
            due = (
                await s.execute(
                    select(Initiation.id)
                    .where(
                        Initiation.status == InitiationStatus.pending,
                        Initiation.next_attempt_at <= datetime.now(UTC),
                    )
                    .order_by(Initiation.id)
                    .limit(5)
                )
            ).scalars().all()
        for cmd_id in due:
            await self._handle(cmd_id)

    async def _handle(self, cmd_id: int) -> None:
        async with self._session_factory() as s:
            cmd = await s.get(Initiation, cmd_id)
        if cmd is None or cmd.status is not InitiationStatus.pending:
            return

        provider = self._sm.get(cmd.account_id)
        if provider is None or not provider.is_connected():
            created = cmd.created_at
            if created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            if (datetime.now(UTC) - created).total_seconds() > _PROVIDER_DEADLINE_SEC:
                await self._fail(cmd_id, "Аккаунт офлайн — переподключите линию")
            else:
                await self._reschedule(cmd_id, delay_sec=10, burn_attempt=False)
            return

        try:
            peer = await provider.resolve_peer(ParsedDest(cmd.dest_kind, cmd.dest_value))
        except NotImplementedError:
            await self._fail(cmd_id, "Канал не поддерживает инициирование")
            return
        except Exception:
            logger.exception("resolve_peer упал: initiation=%s", cmd_id)
            if cmd.attempts + 1 >= _MAX_ATTEMPTS:
                await self._fail(cmd_id, "Сбой канала при поиске — попробуйте ещё раз")
            else:
                await self._reschedule(
                    cmd_id, delay_sec=30 * 2**cmd.attempts, burn_attempt=True
                )
            return
        if peer is None:
            await self._fail(cmd_id, "Не найден или скрыт настройками приватности")
            return

        try:
            await self._apply(cmd.id, peer)
        except IntegrityError:
            # Гонка uq-диалога (параллельный inbound/вторая инициализация):
            # rollback откатил txn целиком — второй проход находит существующий
            # диалог и доигрывает ветку сообщения.
            logger.info("initiation=%s: гонка вставки диалога, повтор", cmd_id)
            await self._apply(cmd.id, peer)

    # ------------------------------------------------------------------ #
    # Одна транзакция применения резолва
    # ------------------------------------------------------------------ #
    async def _apply(self, cmd_id: int, peer: ResolvedPeer) -> None:
        async with self._session_factory() as s:
            cmd = await s.get(Initiation, cmd_id)
            if cmd is None or cmd.status is not InitiationStatus.pending:
                return
            account = await s.get(TgAccount, cmd.account_id)
            assert account is not None  # FK гарантирует наличие

            # Контакт: upsert по (messenger, external_user_id), обогащение
            # только непустыми значениями резолва (не затираем известное).
            contact = (
                await s.execute(
                    select(Contact).where(
                        Contact.messenger == cmd.messenger,
                        Contact.external_user_id == peer.external_user_id,
                    )
                )
            ).scalar_one_or_none()
            if contact is None:
                contact = Contact(
                    messenger=cmd.messenger,
                    external_user_id=peer.external_user_id,
                    name=peer.name,
                    first_name=peer.first_name,
                    last_name=peer.last_name,
                    username=peer.username,
                    phone=peer.phone,
                )
                s.add(contact)
                await s.flush()
            else:
                for field, value in (
                    ("name", peer.name),
                    ("first_name", peer.first_name),
                    ("last_name", peer.last_name),
                    ("username", peer.username),
                    ("phone", peer.phone),
                ):
                    if value and getattr(contact, field) != value:
                        setattr(contact, field, value)

            # Карточка контакта — человеческий источник истины: менеджер
            # инициировал ИЗ неё (перезапись логируется).
            if (
                cmd.entity_type == "contact"
                and cmd.entity_id
                and contact.crm_contact_id != cmd.entity_id
            ):
                logger.info(
                    "initiation=%s: crm_contact_id %s → %s (интент менеджера)",
                    cmd.id, contact.crm_contact_id, cmd.entity_id,
                )
                contact.crm_contact_id = cmd.entity_id

            # Диалог: find-or-create по (chat, messenger, account).
            dialog = (
                await s.execute(
                    select(Dialog).where(
                        Dialog.external_chat_id == peer.external_chat_id,
                        Dialog.messenger == cmd.messenger,
                        Dialog.account_id == cmd.account_id,
                    )
                )
            ).scalar_one_or_none()
            if dialog is None:
                assignee = (
                    account.manager_id
                    if account.manager_id is not None
                    else await line_assignee(s, account)
                )
                dialog = Dialog(
                    contact_id=contact.id,
                    messenger=cmd.messenger,
                    external_chat_id=peer.external_chat_id,
                    account_id=cmd.account_id,
                    assigned_user_id=assignee,
                    # Контактные карточки НЕ пишутся сюда: sync.py трактует
                    # неизвестный crm_entity_type как сделку — привязка
                    # контакта живёт на Contact.crm_contact_id (join).
                    crm_deal_id=cmd.entity_id
                    if cmd.entity_type in ("deal", "lead")
                    else None,
                    crm_entity_type=cmd.entity_type
                    if cmd.entity_type in ("deal", "lead")
                    else None,
                )
                s.add(dialog)
                await s.flush()
            elif cmd.entity_type in ("deal", "lead"):
                # Интент менеджера авторитетен: инициировали из этой
                # карточки — диалог виден в ней (семантика «последнего
                # диалога», как у _resolve_last_dialog в bizproc).
                dialog.crm_deal_id = cmd.entity_id
                dialog.crm_entity_type = cmd.entity_type

            message = Message(
                dialog_id=dialog.id,
                direction=MessageDirection.outbound,
                text=cmd.text,
                status=MessageStatus.pending,
                author_user_id=cmd.author_b24_user_id,
            )
            s.add(message)
            await s.flush()
            dialog.last_msg_at = message.created_at

            await SqlAlchemyOutboxRepository(s).enqueue(
                dialog_id=dialog.id,
                tg_account_id=cmd.account_id,
                external_chat_id=dialog.external_chat_id,
                text=cmd.text,
                is_initiation=not await has_inbound(s, dialog.id),
                message_id=message.id,
            )
            cmd.status = InitiationStatus.linked
            cmd.dialog_id = dialog.id
            await s.commit()
            logger.info(
                "INITIATE: диалог=%s сообщение=%s (%s %s → карточка %s_%s)",
                dialog.id, message.id, cmd.messenger.value, cmd.dest_value,
                cmd.entity_type, cmd.entity_id,
            )

    # ------------------------------------------------------------------ #
    # Статусы
    # ------------------------------------------------------------------ #
    async def _fail(self, cmd_id: int, error: str) -> None:
        async with self._session_factory() as s:
            cmd = await s.get(Initiation, cmd_id)
            if cmd is None or cmd.status is not InitiationStatus.pending:
                return
            cmd.status = InitiationStatus.failed
            cmd.last_error = error[:512]
            await s.commit()
        logger.info("initiation=%s failed: %s", cmd_id, error)

    async def _reschedule(self, cmd_id: int, *, delay_sec: int, burn_attempt: bool) -> None:
        async with self._session_factory() as s:
            cmd = await s.get(Initiation, cmd_id)
            if cmd is None or cmd.status is not InitiationStatus.pending:
                return
            cmd.next_attempt_at = datetime.now(UTC) + timedelta(seconds=delay_sec)
            if burn_attempt:
                cmd.attempts += 1
            await s.commit()
