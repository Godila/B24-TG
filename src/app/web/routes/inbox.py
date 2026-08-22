"""API «Чатов» — общего мессенджера (пункт левого меню B24).

Отдельный префикс /api/inbox: контракт виджета сделки (/api/dialogs) не
трогаем — inbox-списку нужны агрегаты (неотвеченные/непрочитанные) и
supervisor-видимость, которых у виджета нет. История и отправка
переиспользуют существующие GET/POST /api/dialogs/{id}/messages
(GET релаксирован до «владелец, участник линии ИЛИ supervisor»).
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, case, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.b24.notify import crm_card_url
from app.config import get_settings
from app.db import get_session
from app.models import (
    AccountMember,
    Contact,
    Dialog,
    DialogRead,
    DialogStatus,
    LineRole,
    Manager,
    ManagerRole,
    Message,
    MessageDirection,
    Messenger,
)
from app.web.deps import get_current_manager, verify_origin
from app.web.routes.dialogs import _load_dialog_accessible, _visible_dialogs_cond
from app.web.schemas import InboxDialogOut, InboxDialogsPageOut, ReadResultOut

# verify_origin: прод-кука SameSite=none (iframe B24) летит и на
# кросс-сайтовые POST — гашение непрочитанных защищаем как отправку.
router = APIRouter(prefix="/api/inbox", tags=["inbox"], dependencies=[Depends(verify_origin)])

ManagerDep = Annotated[Manager, Depends(get_current_manager)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]


def _crm_url(crm_entity_id: int | None, entity_type: str | None) -> str | None:
    """Ссылка на карточку CRM B24 (сделка/лид) — фронт не знает адрес портала."""
    return crm_card_url(
        get_settings().b24_portal,
        entity_id=crm_entity_id,
        entity_type=entity_type,
        contact_id=None,
    )


def _enum_value(value) -> str:
    """Значение str-enum ('tg'/'max', 'in'/'out') независимо от реализации."""
    return value.value if hasattr(value, "value") else str(value)


@router.get("/dialogs", response_model=InboxDialogsPageOut)
async def list_inbox_dialogs(
    manager: ManagerDep,
    session: SessionDep,
    messenger: Annotated[Messenger | None, Query()] = None,
    assigned: Annotated[int | None, Query(ge=-1)] = None,
    before: int | None = Query(
        default=None,
        description="Ключ-курсор: id диалога-последней строки предыдущей страницы (отдаём старее его)",
    ),
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> InboxDialogsPageOut:
    """Список диалогов для двухпанельного UI «Чатов» (постранично).

    Менеджер видит свои диалоги и диалоги линий своего участия; supervisor
    — все активные диалоги портала (фильтр ``assigned``: id ответственного
    или -1 = без ответственного). Ответ — две секции: ``unanswered`` (ВСЕ
    неотвеченные, кто дольше ждёт — выше) и ``dialogs`` — страница
    отвечавших по свежести (last_msg_at DESC), «Показать ещё» — ключом
    ``before`` (keyset по (last_msg_at, id), стабильнее offset при
    подвижках сортировки). Счётчики считаются на лету портативным SQL
    (group-by + CASE, без DISTINCT ON — прод PostgreSQL, тесты aiosqlite).
    """
    is_supervisor = manager.role == ManagerRole.supervisor

    # Линии, где менеджер — участник с правом записи (для can_write).
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

    # Скоуп видимости + фильтры — общие для всех запросов ниже.
    conds = [Dialog.status == DialogStatus.active]
    if not is_supervisor:
        conds.append(_visible_dialogs_cond(manager))
        # assigned для менеджера бессмыслен: его скоуп уже видимое ему.
    if messenger is not None:
        conds.append(Dialog.messenger == messenger)
    if is_supervisor and assigned is not None:
        conds.append(
            Dialog.assigned_user_id.is_(None)
            if assigned == -1
            else Dialog.assigned_user_id == assigned
        )

    # Курсор: (last_msg_at, id) строки-якоря читаем из САМОЙ строки, а не из
    # параметров клиента — тогда сравнение идёт «хранение с хранением» и не
    # зависит от формата timestamp-строк SQLite (тесты) против PG (прод).
    # Якорь выбираем В СКОПЕ менеджера: голый GET по PK позволял бы перебором
    # чужих id отличать «существует» от «нет» (оракул существования).
    cursor = None
    if before is not None:
        anchor = (
            await session.execute(select(Dialog).where(Dialog.id == before, *conds))
        ).scalar_one_or_none()
        if anchor is None:
            raise HTTPException(status_code=400, detail="Невалидный курсор пагинации")
        cursor = (anchor.last_msg_at, anchor.id)

    # На диалог: id последнего исходящего и id последнего сообщения. Скоуп
    # инлайн (без IN-листа) — агрегат нужен ВСЕМ диалогам скоупа: и секции
    # неотвеченных, и счётчикам страницы.
    last_out_sq = (
        select(
            Message.dialog_id.label("dialog_id"),
            func.max(
                case(
                    (
                        and_(
                            Message.direction == MessageDirection.outbound,
                            # Автоответ — не ответ менеджера: не снимает
                            # «Ожидают ответа» (семантика Wazzup).
                            Message.is_autoreply.is_(False),
                        ),
                        Message.id,
                    )
                )
            ).label("last_out_id"),
            func.max(Message.id).label("last_msg_id"),
        )
        .join(Dialog, Dialog.id == Message.dialog_id)
        .where(*conds)
        .group_by(Message.dialog_id)
        .subquery()
    )
    # Курсор прочтения текущего менеджера: непрочитанные — состояние
    # наблюдателя (участники общего номера и supervisor гасят свои бейджи
    # независимо); COALESCE: нет строки = «не открывал» = всё непрочитано.
    read_sq = (
        select(
            DialogRead.dialog_id.label("dialog_id"),
            DialogRead.last_read_msg_id.label("last_read_msg_id"),
        )
        .where(DialogRead.manager_id == manager.id)
        .subquery()
    )
    # Оба счётчика — условная агрегация: неотвеченные = inbound после
    # последнего исходящего (или все, если исходящих нет); непрочитанные =
    # inbound после курсора менеджера.
    agg_stmt = (
        select(
            last_out_sq.c.dialog_id.label("dialog_id"),
            last_out_sq.c.last_msg_id.label("last_msg_id"),
            func.sum(
                case(
                    (
                        and_(
                            Message.direction == MessageDirection.inbound,
                            Message.id > func.coalesce(last_out_sq.c.last_out_id, 0),
                        ),
                        1,
                    ),
                    else_=0,
                )
            ).label("unanswered"),
            func.sum(
                case(
                    (
                        and_(
                            Message.direction == MessageDirection.inbound,
                            Message.id > func.coalesce(read_sq.c.last_read_msg_id, 0),
                        ),
                        1,
                    ),
                    else_=0,
                )
            ).label("unread"),
        )
        .join(Message, Message.dialog_id == last_out_sq.c.dialog_id)
        .join(Dialog, Dialog.id == last_out_sq.c.dialog_id)
        .outerjoin(read_sq, read_sq.c.dialog_id == last_out_sq.c.dialog_id)
        .group_by(
            last_out_sq.c.dialog_id, last_out_sq.c.last_msg_id, read_sq.c.last_read_msg_id
        )
    )
    agg_rows = (await session.execute(agg_stmt)).all()
    counters = {
        row.dialog_id: (int(row.unanswered or 0), int(row.unread or 0), row.last_msg_id)
        for row in agg_rows
    }
    unanswered_ids = {did for did, (u, _r, _m) in counters.items() if u > 0}

    _rows_stmt = (
        select(Dialog, Contact, Manager)
        .join(Contact, Dialog.contact_id == Contact.id)
        .outerjoin(Manager, Dialog.assigned_user_id == Manager.id)
    )

    # «Ожидают ответа» — все неотвеченные скоупа (обычно немного), старейшее
    # ожидание сверху (линза DESIGN.md).
    unanswered_rows: list = []
    if unanswered_ids:
        unanswered_rows = (
            await session.execute(
                _rows_stmt.where(Dialog.id.in_(unanswered_ids)).order_by(
                    Dialog.last_msg_at.asc().nullslast(), Dialog.id.asc()
                )
            )
        ).all()

    # Страница «Диалогов»: отвечавшие (и «пустые» без сообщений) по свежести.
    page_conds = list(conds)
    if unanswered_ids:
        page_conds.append(Dialog.id.not_in(unanswered_ids))
    if cursor is not None:
        ts, anchor_id = cursor
        if ts is None:
            # Якорь — «пустой» диалог: дальше только NULL-хвост (NULLS LAST),
            # внутри него курсор идёт по id.
            page_conds.append(and_(Dialog.last_msg_at.is_(None), Dialog.id < anchor_id))
        else:
            # NULL-строки «старее» любого ts (NULLS LAST) — третья ветка,
            # иначе они выпадают из всех страниц после первой.
            page_conds.append(
                or_(
                    Dialog.last_msg_at.is_(None),
                    Dialog.last_msg_at < ts,
                    and_(Dialog.last_msg_at == ts, Dialog.id < anchor_id),
                )
            )
    page_rows = (
        await session.execute(
            _rows_stmt.where(*page_conds)
            .order_by(Dialog.last_msg_at.desc().nullslast(), Dialog.id.desc())
            .limit(limit)
        )
    ).all()
    has_more = len(page_rows) == limit

    # Превью последних сообщений одним запросом по PK (обе секции).
    shown_rows = unanswered_rows + page_rows
    last_msg_ids = [
        counters[d.id][2] for d, _c, _m in shown_rows if d.id in counters and counters[d.id][2]
    ]
    last_messages: dict[int, Message] = {}
    if last_msg_ids:
        for msg in (
            await session.execute(select(Message).where(Message.id.in_(last_msg_ids)))
        ).scalars():
            last_messages[msg.dialog_id] = msg

    def _dto(dialog: Dialog, contact: Contact, assignee: Manager | None) -> InboxDialogOut:
        unanswered, unread, _m = counters.get(dialog.id, (0, 0, None))
        last_msg = last_messages.get(dialog.id)
        return InboxDialogOut(
            id=dialog.id,
            contact_id=dialog.contact_id,
            contact_name=contact.name if contact else None,
            messenger=_enum_value(dialog.messenger),
            title=dialog.title,
            crm_deal_id=dialog.crm_deal_id,
            crm_entity_type=dialog.crm_entity_type,
            deal_url=_crm_url(dialog.crm_deal_id, dialog.crm_entity_type),
            last_msg_at=dialog.last_msg_at,
            last_message_direction=(_enum_value(last_msg.direction) if last_msg else None),
            last_message_text=last_msg.text if last_msg else None,
            unanswered_count=unanswered,
            unread_count=unread,
            assigned_manager_id=dialog.assigned_user_id,
            assigned_manager_name=(
                assignee.name if is_supervisor and assignee is not None else None
            ),
            is_mine=dialog.assigned_user_id == manager.id,
            can_write=(
                dialog.assigned_user_id == manager.id
                or dialog.account_id in participant_accounts
            ),
        )

    return InboxDialogsPageOut(
        unanswered=[_dto(d, c, m) for d, c, m in unanswered_rows],
        dialogs=[_dto(d, c, m) for d, c, m in page_rows],
        has_more=has_more,
    )


@router.post("/dialogs/{dialog_id}/read", response_model=ReadResultOut)
async def mark_dialog_read(
    dialog_id: int,
    manager: ManagerDep,
    session: SessionDep,
) -> ReadResultOut:
    """Отметить диалог прочитанным: курсор СВОЙ = MAX(messages.id).

    Любой, кому диалог виден (владелец, участник/наблюдатель линии,
    supervisor), двигает только собственный курсор в dialog_reads —
    чужие бейджи не трогает. Курсор идём только вперёд; пустой диалог
    курсор не меняет.
    """
    await _load_dialog_accessible(session, dialog_id, manager)
    max_id = await session.scalar(
        select(func.max(Message.id)).where(Message.dialog_id == dialog_id)
    )
    cursor = (
        await session.execute(
            select(DialogRead).where(
                DialogRead.dialog_id == dialog_id,
                DialogRead.manager_id == manager.id,
            )
        )
    ).scalar_one_or_none()
    if max_id is None:
        return ReadResultOut(
            dialog_id=dialog_id,
            last_read_msg_id=cursor.last_read_msg_id if cursor else None,
        )
    if cursor is None:
        # ponytail: вставка без ON CONFLICT (диалект-зависимый) — гонка двух
        # параллельных отметок одного менеджера даст 500, перезапрос лечит;
        # per-manager upsert, если замелькает.
        session.add(
            DialogRead(
                dialog_id=dialog_id, manager_id=manager.id, last_read_msg_id=max_id
            )
        )
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            cursor = (
                await session.execute(
                    select(DialogRead).where(
                        DialogRead.dialog_id == dialog_id,
                        DialogRead.manager_id == manager.id,
                    )
                )
            ).scalar_one()
            await _advance_cursor(session, cursor, max_id)
        return ReadResultOut(dialog_id=dialog_id, last_read_msg_id=max_id)
    await _advance_cursor(session, cursor, max_id)
    return ReadResultOut(dialog_id=dialog_id, last_read_msg_id=cursor.last_read_msg_id)


async def _advance_cursor(session: AsyncSession, cursor: DialogRead, max_id: int) -> None:
    """Курсор только вперёд: опоздавший с меньшим MAX не откатит больший."""
    if (cursor.last_read_msg_id or 0) >= max_id:
        return
    await session.execute(
        update(DialogRead)
        .where(
            DialogRead.id == cursor.id,
            func.coalesce(DialogRead.last_read_msg_id, 0) < max_id,
        )
        .values(last_read_msg_id=max_id)
    )
    await session.commit()
    cursor.last_read_msg_id = max_id
