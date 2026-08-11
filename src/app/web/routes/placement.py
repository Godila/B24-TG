"""Placement-обработчик Bitrix24: вкладка чата в карточке сделки (CRM_DEAL_DETAIL_TAB).

B24 открывает этот URL в iFrame и POST'ит form-data:
- PLACEMENT = "CRM_DEAL_DETAIL_TAB"
- PLACEMENT_OPTIONS = JSON {ID: <deal_id>}
- AUTH = JSON {user_id, access_token, member_id, domain, ...}
Handler ставит сессионную куку и отдаёт HTML чат-страницы.

В dev-режиме поддерживается GET с query-параметрами deal_id + b24_user_id
(чтобы открыть виджет локально без реального B24).
"""

import json
import logging

from fastapi import APIRouter, Form, Query
from fastapi.responses import HTMLResponse, JSONResponse

from app.config import get_settings
from app.web.session import create_session_cookie_params

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/placement", tags=["placement"])

_PLACEMENT_CODE = "CRM_DEAL_DETAIL_TAB"


def _chat_html() -> str:
    """Отдать HTML чат-страницы. В Фазе 3 это статика static/placement.html;
    пока возвращаем минимальную заглушку-редирект на статику."""
    # TODO(Фаза 3 Task 7): отдавать реальную placement.html из static/.
    return """<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8"><title>Bitrix-TG Чат</title></head>
<body><div id="chat">Загрузка чата…</div></body></html>"""


def _set_session_and_respond(b24_user_id: int, deal_id: int | None) -> HTMLResponse:
    """Поставить сессионную куку и вернуть HTML чат-страницы."""
    settings = get_settings()
    cookie_params = create_session_cookie_params(
        b24_user_id=b24_user_id, deal_id=deal_id, secret=settings.session_secret,
    )
    resp = HTMLResponse(content=_chat_html())
    resp.set_cookie(**cookie_params)
    return resp


@router.post("/deal", response_model=None)
async def placement_deal_post(
    placement: str = Form(default="", alias="PLACEMENT"),
    placement_options: str = Form(default="{}", alias="PLACEMENT_OPTIONS"),
    auth: str = Form(default="{}", alias="AUTH"),
) -> HTMLResponse | JSONResponse:
    """Реальный placement-вызов от Bitrix24 (POST form-data)."""
    if placement != _PLACEMENT_CODE:
        return JSONResponse(
            {"error": f"unexpected placement: {placement!r}"}, status_code=400,
        )
    try:
        options = json.loads(placement_options) if placement_options else {}
    except json.JSONDecodeError:
        options = {}
    deal_id_raw = options.get("ID")
    deal_id = int(deal_id_raw) if deal_id_raw else None

    try:
        auth_data = json.loads(auth) if auth else {}
    except json.JSONDecodeError:
        auth_data = {}
    user_id_raw = auth_data.get("user_id")
    try:
        b24_user_id = int(user_id_raw)
    except (TypeError, ValueError):
        return JSONResponse({"error": "missing user_id in AUTH"}, status_code=400)

    logger.info(
        "Placement opened: placement=%s deal_id=%s b24_user_id=%s",
        placement, deal_id, b24_user_id,
    )
    return _set_session_and_respond(b24_user_id=b24_user_id, deal_id=deal_id)


@router.get("/deal", response_model=None)
async def placement_deal_dev(
    deal_id: int | None = Query(default=None),
    b24_user_id: int | None = Query(default=None),
) -> HTMLResponse | JSONResponse:
    """Dev-режим: открыть placement локально без B24 POST.

    Доступен только когда settings.dev_mode == True.
    """
    settings = get_settings()
    if not settings.dev_mode:
        return JSONResponse({"error": "dev mode disabled"}, status_code=404)
    if b24_user_id is None:
        return JSONResponse({"error": "b24_user_id required"}, status_code=400)
    return _set_session_and_respond(b24_user_id=b24_user_id, deal_id=deal_id)
