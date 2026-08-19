"""Публичные роуты share-ссылки подключения линии (/connect/<token>).

Страницу открывает владелец телефона БЕЗ какого-либо доступа к ЧатМост:
единственная «авторизация» — знание 256-битного токена из URL (в БД только
sha256-хэш, TTL, revoke админом, гашение по authorized). Capability узкая:
посмотреть статус одного QR-логина и один раз подать 2FA-пароль (транзит).
Куки ЧатМост не участвуют — CSRF-поверхности нет; verify_origin на POST —
эшелон против чужих форм.
"""

import logging
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import update

from app.db import async_session
from app.models import ConnectToken, load_active_connect_token
from app.web.deps import verify_origin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/connect", tags=["connect"], dependencies=[Depends(verify_origin)])

_EXPIRED_PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ссылка недействительна — ЧатМост</title>
<style>
  body { font-family: system-ui, sans-serif; display: grid; place-items: center;
         min-height: 100vh; margin: 0; color: #333; background: #f6f7f9; }
  .card { background: #fff; border-radius: 14px; padding: 32px 40px; max-width: 420px;
          text-align: center; box-shadow: 0 8px 24px rgba(0,0,0,.08); }
  h1 { font-size: 1.2rem; }
</style></head>
<body><div class="card">
  <h1>Ссылка недействительна</h1>
  <p>Она истекла, уже использована или была отменена администратором.</p>
  <p>Попросите администратора отправить новую ссылку подключения.</p>
</div></body></html>"""


class PasswordIn(BaseModel):
    password: str


def _channel_for(messenger):
    from app.web.routes.admin_api import _channels

    channel = _channels.get(messenger)
    if channel is None:
        raise HTTPException(status_code=404, detail="канал не поддерживается")
    return channel


async def _load_token(session, raw: str) -> ConnectToken:
    row = await load_active_connect_token(session, raw)
    if row is None:
        raise HTTPException(status_code=404, detail="ссылка недействительна")
    return row


@router.get("/{token}", response_class=HTMLResponse)
async def connect_page(token: str):
    async with async_session() as s:
        row = await load_active_connect_token(s, token)
    if row is None:
        return HTMLResponse(_EXPIRED_PAGE, status_code=200)
    from app.config import get_settings

    html = (Path(get_settings().static_dir) / "connect.html").read_text(encoding="utf-8")
    # Канальный лейбл/инструкции подставляет сервер: фронт статичен и публичен.
    return HTMLResponse(html.replace("__MESSENGER__", row.messenger.value))


@router.get("/{token}/status", response_model=None)
async def connect_status(token: str):
    view = None
    async with async_session() as s:
        row = await _load_token(s, token)
        view = await _channel_for(row.messenger).login_view(row.account_id)
        if view is not None and view.status.value == "authorized":
            # Ссылка одноразовая по смыслу: аккаунт подключён — гасим.
            await s.execute(
                update(ConnectToken)
                .where(ConnectToken.id == row.id, ConnectToken.used_at.is_(None))
                .values(used_at=datetime.now(UTC))
            )
            await s.commit()
    if view is None:
        raise HTTPException(status_code=404, detail="нет активного логина")
    return view.as_dict()


@router.post("/{token}/password", response_model=None)
async def connect_password(token: str, body: PasswordIn):
    async with async_session() as s:
        row = await _load_token(s, token)
        ok = await _channel_for(row.messenger).submit_password(
            row.account_id, body.password
        )
    if not ok:
        raise HTTPException(
            status_code=409, detail="логин не ждёт пароль (статус не password_required)"
        )
    logger.info("2FA-пароль подан по share-ссылке: account_id=%s", row.account_id)
    return {"status": "submitted"}
