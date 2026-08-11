"""API диалогов и сообщений для Web UI."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bridge.outbox_repo_sqlalchemy import SqlAlchemyOutboxRepository
from app.db import get_session
from app.models import (
    Contact,
    Dialog,
    Manager,
    Message,
    MessageDirection,
    MessageStatus,
    TgAccount,
)
from app.web.deps import get_current_manager
from app.web.schemas import DialogOut, MessageOut, SendMessageIn

router = APIRouter(prefix="/api", tags=["dialogs"])

ManagerDep = Annotated[Manager, Depends(get_current_manager)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]


def _dialog_dto(dialog: Dialog, contact: Contact | None) -> DialogOut:
    messenger = (
        dialog.messenger.value if hasattr(dialog.messenger, "value") else str(dialog.messenger)
    )
    return DialogOut(
        id=dialog.id,
        contact_id=dialog.contact_id,
        contact_name=(contact.name if contact else None),
        messenger=messenger,
        external_chat_id=dialog.external_chat_id,
        crm_deal_id=dialog.crm_deal_id,
        title=dialog.title,
        last_msg_at=dialog.last_msg_at,
    )


def _message_dto(msg: Message) -> MessageOut:
    direction = (
        msg.direction.value if hasattr(msg.direction, "value") else str(msg.direction)
    )
    status = msg.status.value if hasattr(msg.status, "value") else str(msg.status)
    return MessageOut(
        id=msg.id,
        dialog_id=msg.dialog_id,
        direction=direction,
        text=msg.text,
        status=status,
        tg_message_id=msg.tg_message_id,
        author_user_id=msg.author_user_id,
        timeline_comment_id=msg.timeline_comment_id,
        created_at=msg.created_at,
    )


async def _load_dialog_owned(
    session: AsyncSession, dialog_id: int, manager: Manager
) -> Dialog:
    """Диалог существует и принадлежит менеджеру, иначе 404."""
    result = await session.execute(
        select(Dialog).where(
            Dialog.id == dialog_id, Dialog.assigned_user_id == manager.id
        )
    )
    dialog = result.scalar_one_or_none()
    if dialog is None:
        raise HTTPException(status_code=404, detail="Диалог не найден")
    return dialog


@router.get("/dialogs")
async def list_dialogs(
    manager: ManagerDep,
    session: SessionDep,
    deal_id: int | None = Query(default=None),
) -> list[DialogOut]:
    stmt = (
        select(Dialog, Contact)
        .join(Contact, Dialog.contact_id == Contact.id)
        .where(Dialog.assigned_user_id == manager.id)
    )
    if deal_id is not None:
        stmt = stmt.where(Dialog.crm_deal_id == deal_id)
    stmt = stmt.order_by(
        Dialog.last_msg_at.desc().nullslast(), Dialog.id.desc()
    )
    result = await session.execute(stmt)
    return [_dialog_dto(d, c) for d, c in result.all()]


@router.get("/dialogs/{dialog_id}/messages")
async def list_messages(
    dialog_id: int,
    manager: ManagerDep,
    session: SessionDep,
    since: int | None = Query(default=None, description="Вернуть сообщения с id > since"),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[MessageOut]:
    await _load_dialog_owned(session, dialog_id, manager)
    stmt = select(Message).where(Message.dialog_id == dialog_id)
    if since is not None:
        stmt = stmt.where(Message.id > since)
    stmt = stmt.order_by(Message.id.asc()).limit(limit)
    result = await session.execute(stmt)
    return [_message_dto(m) for m in result.scalars().all()]


@router.post("/dialogs/{dialog_id}/messages", status_code=201)
async def send_message(
    dialog_id: int,
    body: SendMessageIn,
    manager: ManagerDep,
    session: SessionDep,
) -> MessageOut:
    dialog = await _load_dialog_owned(session, dialog_id, manager)

    # Аккаунт менеджера (для outbox).
    acc_result = await session.execute(
        select(TgAccount).where(TgAccount.manager_id == manager.id)
    )
    account = acc_result.scalar_one_or_none()
    if account is None:
        raise HTTPException(
            status_code=409,
            detail="У менеджера нет привязанного TG-аккаунта",
        )

    # is_initiation: нет ни одного входящего сообщения в диалоге.
    inbound_exists = await session.execute(
        select(Message.id)
        .where(
            Message.dialog_id == dialog_id,
            Message.direction == MessageDirection.inbound,
        )
        .limit(1)
    )
    is_initiation = inbound_exists.scalar_one_or_none() is None

    message = Message(
        dialog_id=dialog_id,
        direction=MessageDirection.outbound,
        text=body.text,
        status=MessageStatus.pending,
        author_user_id=manager.b24_user_id,
    )
    session.add(message)
    await session.flush()  # получить message.id

    # Обновляем «последнее сообщение» для сортировки списка диалогов.
    dialog.last_msg_at = message.created_at

    repo = SqlAlchemyOutboxRepository(session)
    await repo.enqueue(
        dialog_id=dialog_id,
        tg_account_id=account.id,
        external_chat_id=dialog.external_chat_id,
        text=body.text,
        is_initiation=is_initiation,
    )
    await session.commit()
    return _message_dto(message)
