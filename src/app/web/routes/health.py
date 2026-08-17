"""/health: реальные статусы (план 009).

Раньше возвращал статическое ``{"status": "ok"}`` — bridge мог быть мёртв,
а uptime-мониторинг оставался зелёным. Теперь:

- ``db`` — живость самой БД (SELECT 1);
- ``accounts`` — счётчики статусов TG-аккаунтов. Web-процесс не знает
  ``is_connected`` (это bridge) — bridge-процесс (HealthChecker) персистит
  реальные статусы в ``tg_accounts.status``, web только читает таблицу;
- ``media`` — записываемость медиа-тома (вложения). Информационное: чат
  живёт и без медиа (плейсхолдеры), статус не роняет.

Контракт верхнего уровня ``status``: ok | degraded | error (значения
ok/error сохранены для внешних мониторов; degraded — новое). HTTP:
ok → 200; degraded (нет ни одного active при наличии аккаунтов, либо есть
banned) → 503; БД недоступна → 503 + error/down.
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.media.storage import get_media_storage
from app.models import TgAccount

logger = logging.getLogger(__name__)

router = APIRouter()

AsyncSessionDep = Annotated[AsyncSession, Depends(get_session)]


@router.get("/health")
async def health(session: AsyncSessionDep) -> JSONResponse:
    try:
        await session.execute(select(1))
        accounts = (await session.execute(select(TgAccount))).scalars().all()
    except Exception:
        # Любой сбой БД (нет коннекта/таблиц) → 503 error/down; /health не 500-ит.
        logger.warning("/health: DB check failed", exc_info=True)
        return JSONResponse({"status": "error", "db": "down"}, status_code=503)

    counts: dict[str, int] = {"total": len(accounts), "active": 0, "offline": 0, "banned": 0}
    for account in accounts:
        status_value = account.status.value if account.status else None
        if status_value in counts:
            counts[status_value] += 1

    degraded = (counts["active"] == 0 and counts["total"] > 0) or counts["banned"] > 0
    media_ok = get_media_storage().is_writable()
    return JSONResponse(
        {
            "status": "degraded" if degraded else "ok",
            "db": "ok",
            "accounts": counts,
            "media": {"ok": media_ok},
        },
        status_code=503 if degraded else 200,
    )
