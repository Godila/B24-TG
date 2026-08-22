"""Вебхук событий чат-бота «ЧатМост» (imbot.v2, eventMode=webhook).

Команда dismiss («Отвечать не нужно»): COMMAND-кнопка уведомления шлёт
сюда команду — гасим слоты диалога (UPDATE dismissed_at; сообщения из
чатов вычистит sweep CrmSyncWorker ≤2с) и отвечаем ботом в тот же чат.
Никаких внешних страниц — фидбек там же, где клик.
"""

import logging
from datetime import UTC, datetime

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select, update

import app.db
from app.b24.imbot import KEY_IMBOT_BOT_ID
from app.config import get_settings
from app.models import AppSetting, Dialog, DialogNotification
from app.web.routes.openline import _authorized

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhook/b24", tags=["imbot"])

#: Команда гашения уведомления (scripts/register_imbot.py).
DISMISS_COMMAND = "dismiss"

_REPLY_OK = "✅ Уведомление погашено. Новое входящее сообщение от клиента вернёт его."


def _commands(payload: dict) -> list[dict]:
    """Команды события: data.COMMAND.{COMMAND_ID} → список описаний.

    v2-событие может нести команду плоско (COMMAND/COMMAND_PARAMS прямо в
    data) — принимаем обе формы, парсим толерантно к чужим данным.
    """
    data = payload.get("data")
    if not isinstance(data, dict):
        return []
    block = data.get("COMMAND")
    if isinstance(block, dict):
        return [v for v in block.values() if isinstance(v, dict)]
    if isinstance(block, str):
        return [data]
    return []


def _clicker(payload: dict) -> int | None:
    """user_id кликнувшего: автор сообщения (PARAMS.FROM_USER_ID), фолбэк —
    auth.user_id события."""
    data = payload.get("data")
    params = data.get("PARAMS") if isinstance(data, dict) else None
    if isinstance(params, dict):
        raw = params.get("FROM_USER_ID")
        if str(raw).isdigit():
            return int(raw)
    auth = payload.get("auth")
    raw = auth.get("user_id") if isinstance(auth, dict) else None
    return int(raw) if str(raw).isdigit() else None


@router.post("/imbot")
async def imbot_event(request: Request) -> JSONResponse:
    from app.web.routes.bizproc import _payload_dict

    payload = _payload_dict(await request.body(), request.headers.get("content-type", ""))
    if payload is None:
        return JSONResponse({"error": "validation error"}, status_code=422)

    dismissed: list[tuple[str, int]] = []  # (auth_token, user_id) для ответа бота
    # Обращение через модуль (не from-import): тесты подменяют app.db.async_session.
    async with app.db.async_session() as session:
        if not await _authorized(
            request.headers.get("X-Webhook-Secret"), payload, session
        ):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        auth = payload.get("auth")
        auth_token = auth.get("access_token") if isinstance(auth, dict) else None
        for cmd in _commands(payload):
            if cmd.get("COMMAND") != DISMISS_COMMAND:
                continue
            raw_params = str(cmd.get("COMMAND_PARAMS") or "")
            if not raw_params.isdigit():
                logger.warning("imbot: dismiss без id диалога: %r", raw_params[:32])
                continue
            dialog_id = int(raw_params)
            exists = (
                await session.execute(select(Dialog.id).where(Dialog.id == dialog_id))
            ).scalar_one_or_none()
            if exists is None:
                logger.warning("imbot: dismiss неизвестного диалога %s", dialog_id)
                continue
            await session.execute(
                update(DialogNotification)
                .where(DialogNotification.dialog_id == dialog_id)
                .values(dismissed_at=datetime.now(UTC))
            )
            await session.commit()
            if isinstance(auth_token, str):
                dismissed.append((auth_token, _clicker(payload) or 0))

    logger.info(
        "imbot event %s: %d dismiss command(s)", payload.get("event"), len(dismissed)
    )
    # Ответ ботом кликнувшему — фидбек в том же чате, где был клик. Токен
    # события короткоживущий и scoped imbot; сбой ответа не роняет гашение.
    for token, user_id in dismissed:
        if user_id:
            await _reply_ok(token, user_id)
    return JSONResponse({"status": "ok"})


async def _reply_ok(auth_token: str, user_id: int) -> None:
    settings = get_settings()
    bot_id = settings.imbot_bot_id
    if not bot_id:
        async with app.db.async_session() as s:
            row = (
                await s.execute(
                    select(AppSetting.value).where(AppSetting.key == KEY_IMBOT_BOT_ID)
                )
            ).scalar_one_or_none()
        bot_id = int(row) if row and row.isdigit() else 0
    if not bot_id:
        return  # бот не зарегистрирован — LINK-режим, ответ не нужен
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                settings.b24_portal.rstrip("/") + "/rest/imbot.v2.Chat.Message.send",
                params={"auth": auth_token},
                json={
                    "botId": bot_id,
                    "dialogId": str(user_id),
                    "fields": {"message": _REPLY_OK},
                },
            )
    except httpx.HTTPError:
        logger.warning("imbot: ответ бота не доставлен (сеть)")
