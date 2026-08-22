"""Публичное гашение feed-уведомления — кнопка «Отвечать не нужно».

LINK-кнопка в сообщении уведомления открывает этот URL (менеджер читает
уведомление в B24, сессии ЧатМоста в браузере может не быть). Подпись
HMAC + TTL — как у медиа-ссылок; действие безвредно при утечке (следующее
входящее снова уведомит). Web НЕ зовёт B24-REST: ставит dismissed_at,
сообщения из чатов вычищает sweep CrmSyncWorker (≤ пары секунд).
"""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.b24.notify import verify_dismiss_sig
from app.config import get_settings
from app.db import get_session
from app.models import Dialog, DialogNotification
from app.web.pages import page

router = APIRouter(prefix="/notify", tags=["notify"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


@router.get("/dismiss/{dialog_id}/{exp}/{sig}")
async def dismiss_notification(
    dialog_id: int, exp: int, sig: str, session: SessionDep
):
    settings = get_settings()
    if not verify_dismiss_sig(dialog_id, exp, sig, secret=settings.session_secret):
        # Единый ответ без раскрытия деталей (как public_media).
        return page(
            "Ссылка недействительна",
            "Срок действия ссылки истёк или подпись не совпала. Новое "
            "входящее сообщение принесёт свежее уведомление.",
            status_code=404,
        )
    dialog = (
        await session.execute(select(Dialog).where(Dialog.id == dialog_id))
    ).scalar_one_or_none()
    if dialog is None:
        return page("Диалог не найден", "Уведомление уже погашено.", status_code=404)
    # GET с побочным эффектом: токен в URL защищает действие, менеджер
    # подтвердил намерение самим кликом по кнопке.
    await session.execute(
        update(DialogNotification)
        .where(DialogNotification.dialog_id == dialog_id)
        .values(dismissed_at=datetime.now(UTC))
    )
    await session.commit()
    return page(
        "Уведомление погашено",
        "Строка диалога исчезнет из чата в течение пары секунд. Новое "
        "входящее сообщение от клиента включит уведомления снова.",
    )
