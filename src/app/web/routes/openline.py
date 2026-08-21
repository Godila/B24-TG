"""Вебхук коннектора открытых линий: события ONIMCONNECTOR*.

ONIMCONNECTORMESSAGEADD — оператор написал в нативном чате линии B24:
авторизация эшелонами (секрет-заголовок → application_token → member_id +
self-check токена), дедуп в БД по b24_im_message_id (дубль события = дубль
отправки клиенту — персистентность обязательна), Message+OutboxItem одним
commit → доставляет существующий outbox.

ONIMCONNECTORLINEDELETE / ONIMCONNECTORSTATUSDELETE — линия удалена /
коннектор отключён: чистим привязку аккаунта, очередь crm_sync
возвращается к классическому CRM-синку.

Ответы: мусор/чужое/битое — 200 (ретрай B24 не чинит мусор); DB-сбой —
исключение наружу (500): очередь B24 повторит событие, дедуп пропустит
уже записанное (fail-closed «не терять сообщения»).
"""

import hmac
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.b24.openlines import CONNECTOR_ID, parse_operator_event
from app.bridge.outbox_repo_sqlalchemy import SqlAlchemyOutboxRepository
from app.db import async_session
from app.models import (
    B24Token,
    Dialog,
    Message,
    MessageDirection,
    MessageStatus,
    TgAccount,
)
from app.web.routes.bizproc import (
    _member_token_authorized,
    _payload_dict,
    _redacted,
    _secret_header_ok,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhook/b24", tags=["bitrix24"])


async def _authorized(secret_header: str | None, payload: dict, session: AsyncSession) -> bool:
    """Эшелоны: секрет-заголовок → application_token → member_id + self-check.

    OAuth-токены в событиях опциональны (дока), поэтому сильный идентификатор —
    application_token из установки; до первой переустановки (колонка NULL)
    работает member_id + живой access_token. Всё остальное — 401 fail-closed.
    """
    if _secret_header_ok(secret_header):
        return True
    auth = payload.get("auth")
    if not isinstance(auth, dict):
        return False
    app_token = auth.get("application_token")
    if isinstance(app_token, str) and app_token:
        token_row = (
            await session.execute(select(B24Token).limit(1))
        ).scalar_one_or_none()
        if token_row is not None and token_row.application_token and hmac.compare_digest(
            # Байты: compare_digest(str, str) падает TypeError на не-ASCII.
            app_token.encode("utf-8"), token_row.application_token.encode("utf-8")
        ):
            return True
    member_id = auth.get("member_id")
    access_token = auth.get("access_token")
    if not (isinstance(member_id, str) and isinstance(access_token, str) and access_token):
        return False
    return await _member_token_authorized(session, member_id, access_token)


async def _handle_line_delete(session: AsyncSession, payload: dict, *, full: bool) -> None:
    """LINEDELETE: привязка сносится (аккаунт → классический CRM-синк);
    STATUSDELETE: ol_active=False — очередь ждёт реактивации слайдером."""
    data = payload.get("data")
    if not isinstance(data, dict):
        return
    # STATUSDELETE-подписка приложения глобальна: событие чужого коннектора
    # на той же линии не должно глушить нашу привязку (LINEDELETE чистит
    # безусловно — линия удалена целиком, коннекторам там нечего терять).
    if not full and data.get("CONNECTOR") != CONNECTOR_ID:
        logger.info(
            "IMCONNECTOR STATUSDELETE чужого коннектора %r — игнор", data.get("CONNECTOR")
        )
        return
    line = data.get("LINE")
    if isinstance(line, (int, str)) and str(line) != "":
        line = str(line)
    else:
        return
    values: dict = {"ol_active": False}
    if full:
        values["ol_line_id"] = None
    result = await session.execute(
        update(TgAccount).where(TgAccount.ol_line_id == line).values(**values)
    )
    await session.commit()
    logger.info(
        "IMCONNECTOR %s: линия %s — привязка обновлена (%s строк)",
        "LINEDELETE" if full else "STATUSDELETE",
        line,
        result.rowcount,
    )


async def handle_operator_event(ev, session: AsyncSession) -> JSONResponse:
    """Валидное событие → Message+OutboxItem (по элементам, дедуп в БД).

    Дедуб — SELECT + unique-индекс (dialog_id, b24_im_message_id): гонка
    двух доставок одного события закрыта IntegrityError на flush.
    Каждый элемент — своя транзакция: сбой одного не откатывает остальные.
    """
    account = (
        await session.execute(
            select(TgAccount).where(
                TgAccount.ol_line_id == ev.line, TgAccount.ol_active.is_(True)
            )
        )
    ).scalar_one_or_none()
    if account is None:
        logger.warning("IMCONNECTOR: линия %s не привязана/неактивна — игнор", ev.line)
        return JSONResponse({"status": "no_binding"})
    repo = SqlAlchemyOutboxRepository(session)
    queued = skipped = 0
    for msg in ev.messages:
        # v1 — текст: файлы оператора в событии не документированы
        # (ponytail: media-отправка, когда живой лог покажет их формат).
        if not msg.text:
            logger.warning(
                "IMCONNECTOR: пустой текст (файл?) im.message_id=%s — skip",
                msg.im_message_id,
            )
            skipped += 1
            continue
        try:
            dialog_id = int(msg.chat_id)
        except ValueError:
            logger.warning("IMCONNECTOR: chat.id=%r не наш — skip", msg.chat_id)
            skipped += 1
            continue
        dup = await session.execute(
            select(Message.id).where(
                Message.dialog_id == dialog_id,
                Message.b24_im_message_id == msg.im_message_id,
            )
        )
        if dup.scalar_one_or_none() is not None:
            skipped += 1
            continue
        dialog = await session.get(Dialog, dialog_id)
        if dialog is None or dialog.account_id != account.id:
            logger.warning(
                "IMCONNECTOR: диалог %s не принадлежит линии аккаунта %s — skip",
                dialog_id,
                account.id,
            )
            skipped += 1
            continue
        message = Message(
            dialog_id=dialog_id,
            direction=MessageDirection.outbound,
            text=msg.text,
            status=MessageStatus.pending,
            author_user_id=msg.user_id,
            b24_im_chat_id=msg.im_chat_id,
            b24_im_message_id=msg.im_message_id,
        )
        session.add(message)
        try:
            await session.flush()
        except IntegrityError:
            # Гонка дубля события: второйINSERT по unique-индексу — это skip.
            await session.rollback()
            skipped += 1
            continue
        await repo.enqueue(
            dialog_id=dialog_id,
            tg_account_id=account.id,
            external_chat_id=dialog.external_chat_id,
            text=msg.text,
            is_initiation=False,
            message_id=message.id,
        )
        # Маркер свежести диалога (списки сортируют по last_msg_at) — как
        # во всех путях создания Message (incoming/dialogs/bizproc).
        dialog.last_msg_at = message.created_at
        await session.commit()
        queued += 1
    logger.info(
        "IMCONNECTOR: линия %s — queued=%s skipped=%s", ev.line, queued, skipped
    )
    return JSONResponse({"status": "queued", "queued": queued, "skipped": skipped})


async def _capture_application_token(payload: dict, session: AsyncSession) -> None:
    """Захват application_token из УЖЕ верифицированного события.

    События B24 несут его всегда, а ONAPPINSTALL мог пройти мимо (живой
    кейс 08-21: переустановка при старом коде — 422). Без него события
    без access_token уходят в 401 fail-closed. Заполняем только NULL:
    переустановка — авторитетный источник, чужое значение не перетираем.
    """
    auth = payload.get("auth")
    if not isinstance(auth, dict):
        return
    app_token = auth.get("application_token")
    if not isinstance(app_token, str) or not app_token:
        return
    token_row = (await session.execute(select(B24Token).limit(1))).scalar_one_or_none()
    if token_row is not None and token_row.application_token is None:
        token_row.application_token = app_token
        await session.commit()
        logger.info("IMCONNECTOR: application_token захвачен из события")


@router.post("/imconnector")
async def imconnector_event(request: Request) -> JSONResponse:
    """Приём событий коннектора (JSON или form-php; тело логируется redacted)."""
    payload = _payload_dict(await request.body(), request.headers.get("content-type", ""))
    if payload is None:
        logger.warning("IMCONNECTOR: malformed body rejected (не JSON/form)")
        return JSONResponse({"error": "validation error"}, status_code=422)
    logger.info("IMCONNECTOR payload: %s", _redacted(payload))
    async with async_session() as session:
        if not await _authorized(request.headers.get("X-Webhook-Secret"), payload, session):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        await _capture_application_token(payload, session)
        event = payload.get("event")
        if event in ("ONIMCONNECTORLINEDELETE", "ONIMCONNECTORSTATUSDELETE"):
            await _handle_line_delete(
                session, payload, full=event == "ONIMCONNECTORLINEDELETE"
            )
            return JSONResponse({"status": "ok"})
        if event != "ONIMCONNECTORMESSAGEADD":
            logger.info("IMCONNECTOR: чужое событие %r — игнор", event)
            return JSONResponse({"status": "ignored"})
        ev = parse_operator_event(payload)
        if ev is None or ev.connector != CONNECTOR_ID:
            logger.info("IMCONNECTOR: payload не распознан или чужой коннектор — игнор")
            return JSONResponse({"status": "ignored"})
        return await handle_operator_event(ev, session)
