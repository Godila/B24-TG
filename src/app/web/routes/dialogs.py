"""API диалогов и сообщений для Web UI."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.bridge.outbox_repo_sqlalchemy import SqlAlchemyOutboxRepository
from app.db import get_session
from app.media.storage import (
    MediaPathError,
    MediaTooLargeError,
    attachment_type_for,
    ext_for,
    get_media_storage,
    mime_allowed_for_upload,
    normalize_mime,
    sanitize_file_name,
    serve_mime,
)
from app.messaging.resolve import normalize_dest
from app.messaging.types import MEDIA_PLACEHOLDERS
from app.models import (
    AccountMember,
    Attachment,
    Contact,
    Dialog,
    Initiation,
    InitiationStatus,
    LineRole,
    Manager,
    ManagerRole,
    Message,
    MessageDirection,
    MessageStatus,
    TgAccount,
    TgAccountStatus,
    has_inbound,
)
from app.web.deps import get_current_manager, verify_origin
from app.web.routes.placement import _account_label
from app.web.schemas import (
    AccountOut,
    AttachmentOut,
    DialogOut,
    InitiateIn,
    InitiationOut,
    MessageOut,
    SendMessageIn,
)

# verify_origin: прод-кука SameSite=none (iframe B24) прикрепляется к
# кросс-сайтовым POST — без сверки Origin открыт был бы POST сообщений.
router = APIRouter(prefix="/api", tags=["dialogs"], dependencies=[Depends(verify_origin)])

logger = logging.getLogger(__name__)

ManagerDep = Annotated[Manager, Depends(get_current_manager)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]

#: Тексты, под которыми «пустое» медиа-сообщение живёт в БД (их видят
#: B24-timeline и превью списка «Чатов»); в пузыре при вложении не нужны.
_MEDIA_TEXT_PLACEHOLDERS = frozenset(MEDIA_PLACEHOLDERS.values()) | {"[вложение]"}


def _dialog_dto(
    dialog: Dialog, contact: Contact | None, *, can_write: bool = True
) -> DialogOut:
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
        can_write=can_write,
    )


def _message_dto(msg: Message) -> MessageOut:
    direction = msg.direction.value if hasattr(msg.direction, "value") else str(msg.direction)
    status = msg.status.value if hasattr(msg.status, "value") else str(msg.status)
    # Незагруженная коллекция (свежее исходящее в роуте) = пустая: доступ
    # к ней в async-контексте роняет MissingGreenlet.
    attachments = [] if "attachments" in sa_inspect(msg).unloaded else (msg.attachments or [])
    text = msg.text
    if attachments and text in _MEDIA_TEXT_PLACEHOLDERS:
        text = None
    return MessageOut(
        id=msg.id,
        dialog_id=msg.dialog_id,
        direction=direction,
        text=text,
        status=status,
        external_message_id=msg.external_message_id,
        author_user_id=msg.author_user_id,
        is_autoreply=bool(msg.is_autoreply),
        timeline_comment_id=msg.timeline_comment_id,
        created_at=msg.created_at,
        attachments=[
            AttachmentOut(
                id=a.id,
                type=a.type.value if hasattr(a.type, "value") else str(a.type),
                mime_type=a.mime_type,
                size=a.size,
                file_name=a.file_name,
                file_url=f"/api/attachments/{a.id}/file",
            )
            for a in attachments
        ],
    )


def _visible_dialogs_cond(manager: Manager):
    """Скоуп видимости не-supervisor: свои диалогы ИЛИ диалоги линий,
    где менеджер — участник (любой роли). Supervisor видит всё — без скоупа."""
    return or_(
        Dialog.assigned_user_id == manager.id,
        Dialog.account_id.in_(
            select(AccountMember.account_id).where(
                AccountMember.manager_id == manager.id
            )
        ),
    )


async def _line_role(
    session: AsyncSession, dialog: Dialog, manager: Manager
) -> LineRole | None:
    """Роль менеджера в линии диалога (None — не участник)."""
    if dialog.account_id is None:
        return None
    return await session.scalar(
        select(AccountMember.role).where(
            AccountMember.account_id == dialog.account_id,
            AccountMember.manager_id == manager.id,
        )
    )


async def _load_dialog_accessible(
    session: AsyncSession, dialog_id: int, manager: Manager
) -> Dialog:
    """Диалог существует и ВИДЕН менеджеру, иначе 404.

    Видимость: владелец, участник линии диалога (участник/наблюдатель) ИЛИ
    supervisor (надзор — читает все диалоги портала, включая неназначенные).
    404, а не 403, для невидимых — не раскрываем существование чужих
    диалогов (контракт виджета сделки).
    """
    dialog = (
        await session.execute(select(Dialog).where(Dialog.id == dialog_id))
    ).scalar_one_or_none()
    if dialog is None:
        raise HTTPException(status_code=404, detail="Диалог не найден")
    if (
        dialog.assigned_user_id != manager.id
        and manager.role != ManagerRole.supervisor
        and await _line_role(session, dialog, manager) is None
    ):
        raise HTTPException(status_code=404, detail="Диалог не найден")
    return dialog


@router.get("/me", response_model=None)
async def whoami(manager: ManagerDep) -> dict:
    """Профиль текущего менеджера: виджет прячет composer при is_readonly,
    админ-страница узнаёт роль."""
    return {
        "id": manager.id,
        "name": manager.name,
        "b24_user_id": manager.b24_user_id,
        "role": manager.role.value,
        "is_readonly": manager.is_readonly,
    }


@router.get("/dialogs")
async def list_dialogs(
    manager: ManagerDep,
    session: SessionDep,
    deal_id: int | None = Query(default=None),
    entity_type: str = Query(default="deal", pattern="^(deal|lead|contact)$"),
) -> list[DialogOut]:
    stmt = (
        select(Dialog, Contact)
        .join(Contact, Dialog.contact_id == Contact.id)
        .where(_visible_dialogs_cond(manager))
    )
    if deal_id is not None:
        # id-пространства сущностей независимы — фильтруем и типом;
        # legacy-строки без crm_entity_type считаются сделками.
        if entity_type == "lead":
            stmt = stmt.where(Dialog.crm_deal_id == deal_id, Dialog.crm_entity_type == "lead")
        elif entity_type == "contact":
            # Контактная карточка: диалоги через живую привязку контакта
            # (crm_entity_type='contact' не пишется — см. initiate_dialog).
            stmt = stmt.where(Contact.crm_contact_id == deal_id)
        else:
            stmt = stmt.where(
                Dialog.crm_deal_id == deal_id,
                or_(Dialog.crm_entity_type == "deal", Dialog.crm_entity_type.is_(None)),
            )
    stmt = stmt.order_by(Dialog.last_msg_at.desc().nullslast(), Dialog.id.desc())
    result = await session.execute(stmt)
    participant_accounts = set(
        (
            await session.execute(
                select(AccountMember.account_id).where(
                    AccountMember.manager_id == manager.id,
                    AccountMember.role == LineRole.participant,
                )
            )
        )
        .scalars()
        .all()
    )
    return [
        _dialog_dto(
            d,
            c,
            can_write=(
                d.assigned_user_id == manager.id or d.account_id in participant_accounts
            ),
        )
        for d, c in result.all()
    ]


@router.get("/dialogs/{dialog_id}/messages")
async def list_messages(
    dialog_id: int,
    manager: ManagerDep,
    session: SessionDep,
    since: int | None = Query(default=None, description="Вернуть сообщения с id > since"),
    before: int | None = Query(
        default=None, description="Вернуть сообщения с id < before (страница старее)"
    ),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[MessageOut]:
    """История сообщений.

    Режимы:
    - ``since`` — poll новых сообщений: ASC от курсора (контракт виджета);
    - ``before`` — страница истории старее курсора: DESC (новейшие из старых);
    - без параметров — первичная загрузка: DESC (новейшие N), UI разворачивает сам.
    """
    await _load_dialog_accessible(session, dialog_id, manager)
    # selectinload: poll каждые 3с не должен рождать N+1 по вложениям.
    stmt = (
        select(Message)
        .options(selectinload(Message.attachments))
        .where(Message.dialog_id == dialog_id)
    )
    if since is not None:
        stmt = stmt.where(Message.id > since).order_by(Message.id.asc())
    else:
        if before is not None:
            stmt = stmt.where(Message.id < before)
        stmt = stmt.order_by(Message.id.desc())
    stmt = stmt.limit(limit)
    result = await session.execute(stmt)
    return [_message_dto(m) for m in result.scalars().all()]


async def _outbound_context(
    session: AsyncSession, dialog_id: int, manager: Manager
) -> tuple[Dialog, TgAccount, bool]:
    """Общие guard'ы отправки (текст и медиа): доступ → право записи →
    read-only → аккаунт линии → is_initiation. Коды ошибок — контракт UI."""
    dialog = await _load_dialog_accessible(session, dialog_id, manager)

    # Пишет ответственный ИЛИ участник линии (общий номер). Наблюдатель и
    # supervisor-надзор — 403: они знают о существовании диалога из списка,
    # UI нужен явный сигнал прятать composer.
    role = await _line_role(session, dialog, manager)
    if (
        dialog.assigned_user_id != manager.id
        and role != LineRole.participant
    ):
        raise HTTPException(
            status_code=403, detail="Писать можно только в свои диалоги или как участник линии"
        )

    # Права: read-only менеджер читает историю, но не отправляет.
    if manager.is_readonly:
        raise HTTPException(status_code=403, detail="Режим только чтение: отправка запрещена")

    # Аккаунт ЛИНИИ диалога (общий номер отвечает с того же номера, с
    # которого писал клиент); фолбэк — личный аккаунт менеджера (легаси-
    # диалоги без линии).
    account = (
        await session.get(TgAccount, dialog.account_id)
        if dialog.account_id is not None
        else None
    )
    if account is None:
        acc_result = await session.execute(
            select(TgAccount).where(
                TgAccount.manager_id == manager.id,
                TgAccount.messenger == dialog.messenger,
            )
        )
        account = acc_result.scalar_one_or_none()
    if account is None:
        channel_label = (
            dialog.messenger.value if hasattr(dialog.messenger, "value") else str(dialog.messenger)
        )
        raise HTTPException(
            status_code=409,
            detail=f"У линии диалога нет подключённого аккаунта {channel_label.upper()}",
        )

    # is_initiation: нет ни одного входящего сообщения в диалоге.
    is_initiation = not await has_inbound(session, dialog_id)
    return dialog, account, is_initiation


# --------------------------------------------------------------------- #
# «Написать первым»: резолв исполняет bridge-воркер (провайдеры живут
# только там), web пишет команду и поллит статус.
# --------------------------------------------------------------------- #


async def _writable_accounts(session: AsyncSession, manager: Manager) -> list[TgAccount]:
    """Активные аккаунты, с которых менеджер может инициировать: личные
    (manager_id) ИЛИ participant линий. Supervisor — все активные."""
    stmt = select(TgAccount).where(
        TgAccount.status == TgAccountStatus.active,
        TgAccount.is_removed.is_(False),
    )
    if manager.role != ManagerRole.supervisor:
        stmt = stmt.where(
            or_(
                TgAccount.manager_id == manager.id,
                TgAccount.id.in_(
                    select(AccountMember.account_id).where(
                        AccountMember.manager_id == manager.id,
                        AccountMember.role == LineRole.participant,
                    )
                ),
            )
        )
    return list((await session.execute(stmt)).scalars().all())


@router.get("/accounts")
async def list_accounts(
    manager: ManagerDep,
    session: SessionDep,
) -> list[AccountOut]:
    """Селектор аккаунтов формы «написать первым» (+ флаг приоритетного);
    фильтрацию по каналу делает фронт (клиентских аккаунтов мало)."""
    accounts = await _writable_accounts(session, manager)
    defaults = manager.default_outbound or {}
    return [
        AccountOut(
            id=a.id,
            messenger=a.messenger.value,
            label=_account_label(a),
            is_default=defaults.get(a.messenger.value) == a.id,
        )
        for a in accounts
    ]


@router.post("/dialogs/initiate", status_code=202)
async def initiate_dialog(
    body: InitiateIn, manager: ManagerDep, session: SessionDep
) -> InitiationOut:
    """Поставить команду «написать первым»: 202 + id, результат — поллингом."""
    if manager.is_readonly:
        raise HTTPException(status_code=403, detail="Режим только чтение: отправка запрещена")

    accounts = [
        a for a in await _writable_accounts(session, manager)
        if a.messenger.value == body.messenger
    ]
    if body.account_id is not None:
        account = next((a for a in accounts if a.id == body.account_id), None)
        if account is None:
            raise HTTPException(status_code=404, detail="Аккаунт не найден или недоступен")
    else:
        # Приоритетный → единственный доступный → 409 (выберите аккаунт).
        default_id = (manager.default_outbound or {}).get(body.messenger)
        account = next((a for a in accounts if a.id == default_id), None)
        if account is None and len(accounts) == 1:
            account = accounts[0]
        if account is None:
            raise HTTPException(
                status_code=409,
                detail=f"Не выбран аккаунт {body.messenger.upper()} для отправки",
            )

    dest = normalize_dest(account.messenger, body.dest)
    if dest is None:
        raise HTTPException(
            status_code=422,
            detail="Укажите телефон в формате +7… или @username (только Telegram)",
        )

    cmd = Initiation(
        account_id=account.id,
        messenger=account.messenger,
        author_manager_id=manager.id,
        author_b24_user_id=manager.b24_user_id,
        entity_type=body.entity_type,
        entity_id=body.entity_id,
        dest_kind=dest.kind,
        dest_value=dest.value,
        text=body.text,
    )
    session.add(cmd)
    if body.remember_account:
        defaults = dict(manager.default_outbound or {})
        defaults[body.messenger] = account.id
        manager.default_outbound = defaults
    try:
        await session.commit()
    except IntegrityError:
        # uq_initiations_active: дубль двойного клика по тому же номеру.
        await session.rollback()
        raise HTTPException(
            status_code=409, detail="Этот номер уже в обработке — дождитесь результата"
        ) from None
    return InitiationOut(id=cmd.id, status=InitiationStatus.pending.value)


def _first_crm_phone(fields: object) -> str | None:
    """Первый PHONE из ответа crm.*.get: [{ID, VALUE, VALUE_TYPE}, …]."""
    phones = fields.get("PHONE") if isinstance(fields, dict) else None
    if isinstance(phones, list):
        for p in phones:
            if isinstance(p, dict) and p.get("VALUE"):
                return str(p["VALUE"]).strip()
    return None


async def _b24_entity_phone(entity_type: str, entity_id: int) -> str | None:
    """Телефон клиента из карточки B24 (сделка → контакт по связи).

    Одноразовый клиент на запрос — паттерн admin_api/placement (web-процесс
    не держит общий B24-клиент). Любой сбой → None: prefill fail-open.
    """
    from app.b24.client import Bitrix24Client
    from app.b24.token_manager import TokenManager
    from app.config import get_settings

    settings = get_settings()
    token = await TokenManager(
        client_id=settings.b24_client_id,
        client_secret=settings.b24_client_secret,
    ).get_token()
    if token is None:
        return None
    client = Bitrix24Client(
        client_endpoint=token.client_endpoint or settings.b24_portal.rstrip("/") + "/rest/",
        min_interval=settings.b24_min_call_interval,
    )
    try:
        auth = token.access_token
        if entity_type == "deal":
            deal = await client.call("crm.deal.get", auth_token=auth, params={"ID": entity_id})
            contact_ids = deal.get("CONTACT_IDS") if isinstance(deal, dict) else None
            if not contact_ids:
                return None
            fields = await client.call(
                "crm.contact.get", auth_token=auth, params={"ID": contact_ids[0]}
            )
        else:
            method = "crm.contact.get" if entity_type == "contact" else "crm.lead.get"
            fields = await client.call(method, auth_token=auth, params={"ID": entity_id})
        return _first_crm_phone(fields)
    except Exception:
        logger.info("prefill: не удалось получить телефон из B24", exc_info=True)
        return None
    finally:
        await client.aclose()


@router.get("/dialogs/initiate/prefill")
async def initiate_prefill(
    manager: ManagerDep,
    session: SessionDep,
    entity_type: str = Query(pattern="^(deal|lead|contact)$"),
    entity_id: int = Query(gt=0),
) -> dict:
    """Телефон клиента для предзаполнения «Кому» (суть фичи: телефон уже
    в карточке, менеджер не должен перепечатывать). Быстрый путь — наш
    Contact по привязке карточки; иначе 1-2 вызова B24. fail-open: null."""
    phone: str | None = None
    if entity_type == "contact":
        phone = (
            await session.execute(
                select(Contact.phone)
                .where(Contact.crm_contact_id == entity_id, Contact.phone.is_not(None))
                .limit(1)
            )
        ).scalar_one_or_none()
    if not phone:
        phone = await _b24_entity_phone(entity_type, entity_id)
    return {"phone": phone}


@router.get("/dialogs/initiate/{cmd_id}")
async def initiation_status(
    cmd_id: int, manager: ManagerDep, session: SessionDep
) -> InitiationOut:
    """Статус инициализации (полл виджета): автор или supervisor, иначе 404."""
    cmd = await session.get(Initiation, cmd_id)
    if cmd is None or (
        cmd.author_manager_id != manager.id and manager.role != ManagerRole.supervisor
    ):
        raise HTTPException(status_code=404, detail="Инициализация не найдена")
    return InitiationOut(
        id=cmd.id,
        status=cmd.status.value,
        dialog_id=cmd.dialog_id,
        error=cmd.last_error,
    )


@router.post("/dialogs/{dialog_id}/messages", status_code=201)
async def send_message(
    dialog_id: int,
    body: SendMessageIn,
    manager: ManagerDep,
    session: SessionDep,
) -> MessageOut:
    dialog, account, is_initiation = await _outbound_context(session, dialog_id, manager)

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
        message_id=message.id,
    )
    await session.commit()
    return _message_dto(message)


@router.post("/dialogs/{dialog_id}/media", status_code=201)
async def send_media_message(
    dialog_id: int,
    manager: ManagerDep,
    session: SessionDep,
    file: Annotated[UploadFile, File()],
    caption: Annotated[str, Form(max_length=1024)] = "",
) -> MessageOut:
    """Отправить медиа-вложение (multipart: file + необяз. caption).

    Отдельный эндпоинт от POST /messages (JSON): контракт текстовой отправки
    не меняется. Порядок «файл на диск → строки в БД → commit» — воркер
    никогда не увидит элемент очереди без файла (обратное — файл-сирота,
    невидимая утечка диска).
    """
    dialog, account, is_initiation = await _outbound_context(session, dialog_id, manager)

    mime = normalize_mime(file.content_type)
    if not mime_allowed_for_upload(mime):
        raise HTTPException(status_code=415, detail="Формат файла не поддерживается")

    data = await file.read()
    storage = get_media_storage()
    safe_name = sanitize_file_name(file.filename)
    try:
        stored = storage.save_bytes(data, direction="out", ext=ext_for(safe_name, mime))
    except MediaTooLargeError:
        raise HTTPException(status_code=413, detail="Файл больше допустимого размера") from None
    finally:
        await file.close()

    # Caption может быть пустым; плейсхолдер типа уходит в B24-timeline
    # и превью списка «Чатов» (DTO пузыря его скроет).
    attachment_type = attachment_type_for(mime)
    text = caption.strip() or MEDIA_PLACEHOLDERS.get(attachment_type, "[файл]")

    message = Message(
        dialog_id=dialog_id,
        direction=MessageDirection.outbound,
        text=text,
        status=MessageStatus.pending,
        author_user_id=manager.b24_user_id,
    )
    session.add(message)
    attachment = Attachment(
        type=attachment_type,
        file_path=stored.relative_path,
        mime_type=mime,
        size=stored.size,
        file_name=safe_name,
    )
    # Append ДО flush: у transient-объекта коллекция инициализируется без
    # lazy-load (после flush — MissingGreenlet), каскад сам поставит FK.
    message.attachments.append(attachment)
    await session.flush()  # получить message.id и attachment.id

    dialog.last_msg_at = message.created_at

    repo = SqlAlchemyOutboxRepository(session)
    await repo.enqueue(
        dialog_id=dialog_id,
        tg_account_id=account.id,
        external_chat_id=dialog.external_chat_id,
        # В очередь — реальный caption (может быть пустым): плейсхолдер
        # живёт только в Message.text для B24/превью, клиенту в TG он
        # подписью быть не должен.
        text=caption.strip(),
        is_initiation=is_initiation,
        message_id=message.id,
        attachment_id=attachment.id,
    )
    await session.commit()
    return _message_dto(message)


@router.get("/attachments/{attachment_id}/file")
async def download_attachment(
    attachment_id: int,
    manager: ManagerDep,
    session: SessionDep,
) -> FileResponse:
    """Раздача вложения: только владелец диалога или supervisor.

    Медиа клиентов — PII: доступ через сессионную куку + контракт
    видимости диалога (404 чужим — не раскрываем существование), кэш
    приватный. Inline рендерятся только MIME из безопасного списка,
    остальное качается как attachment (octet-stream).
    """
    row = (
        await session.execute(
            select(Attachment, Message)
            .join(Message, Attachment.message_id == Message.id)
            .where(Attachment.id == attachment_id)
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Вложение не найдено")
    attachment, message = row
    await _load_dialog_accessible(session, message.dialog_id, manager)

    try:
        path = get_media_storage().abs_path(attachment.file_path)
    except MediaPathError:
        logger.warning("attachment id=%s broken file_path=%r", attachment.id, attachment.file_path)
        raise HTTPException(status_code=404, detail="Файл не найден") from None
    if not path.is_file():
        logger.warning(
            "attachment row without file: id=%s path=%s", attachment.id, attachment.file_path
        )
        raise HTTPException(status_code=404, detail="Файл не найден")

    media_type, inline = serve_mime(attachment.mime_type)
    return FileResponse(
        path,
        media_type=media_type,
        filename=attachment.file_name or path.name,
        content_disposition_type="inline" if inline else "attachment",
        headers={"Cache-Control": "private, max-age=86400"},
    )
