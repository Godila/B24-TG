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
from pathlib import Path

from fastapi import APIRouter, Form, Query
from fastapi.responses import HTMLResponse, JSONResponse

from app.b24.client import Bitrix24Client, Bitrix24Error
from app.config import get_settings
from app.web.session import create_session_cookie_params

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/placement", tags=["placement"])

_PLACEMENT_CODE = "CRM_DEAL_DETAIL_TAB"


async def _verify_b24_token(access_token: str, expected_user_id: int) -> bool:
    """Проверить, что access_token валиден и принадлежит expected_user_id.

    Bitrix24 передаёт вместе с placement реальный access_token. Вызов
    ``user.current`` подтверждает, что токен: (1) валиден, (2) не подделан,
    (3) соответствует заявленному user_id. Без этой проверки злоумышленник
    мог бы отправить POST с произвольным user_id и получить сессионную куку
    любого менеджера.
    """
    if not access_token:
        return False
    settings = get_settings()
    client = Bitrix24Client(client_endpoint=settings.b24_portal.rstrip("/") + "/rest/")
    try:
        result = await client.call("user.current", auth_token=access_token)
    except Bitrix24Error:
        logger.warning("placement: invalid B24 access_token rejected")
        return False
    except Exception:
        logger.exception("placement: B24 token verification failed")
        return False
    finally:
        # Клиент одноразовый на placement-вход и держит свой httpx-пул —
        # обязательно закрываем, иначе утечка коннектов на каждый логин.
        await client.aclose()
    if not isinstance(result, dict):
        return False
    try:
        return int(result.get("ID", 0)) == expected_user_id
    except (TypeError, ValueError):
        return False


def _chat_html() -> str:
    """Прочитать static/placement.html с диска. Если файла нет — заглушка."""
    settings = get_settings()
    html_path = Path(settings.static_dir) / "placement.html"
    if html_path.is_file():
        return html_path.read_text(encoding="utf-8")
    logger.warning("placement.html not found at %s — returning stub", html_path)
    return (
        '<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">'
        "<title>Bitrix-TG Чат</title></head>"
        '<body><div id="chat">Чат недоступен: static/placement.html не найден.</div>'
        "</body></html>"
    )


def _set_session_and_respond(
    b24_user_id: int, deal_id: int | None
) -> HTMLResponse | JSONResponse:
    """Поставить сессионную куку и вернуть HTML чат-страницы.

    Кука ставится в том же ответе, что и HTML — важно для iFrame (редирект
    на /static/ внутри iFrame мог бы потерять SameSite-контекст).
    """
    settings = get_settings()
    cookie_params = create_session_cookie_params(
        b24_user_id=b24_user_id, deal_id=deal_id, secret=settings.session_secret,
        secure=not settings.dev_mode,
    )
    # deal_id добавляем в URL как query — фронт читает его для фильтра диалогов.
    body = _chat_html()
    if deal_id is not None:
        # Внедряем deal_id через <base> не нужно; фронт читает window.location.
        # Но placement.html отдаётся как есть — фронт берёт ?deal_id= из URL.
        # Здесь мы отдаём HTML напрямую (не через redirect), поэтому добавим
        # deal_id как data-атрибут, который app.js прочтёт.
        marker = "<body>"
        inject = f'<body data-deal-id="{deal_id}">'
        body = body.replace(marker, inject, 1)
    resp = HTMLResponse(content=body)
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

    # Безопасность: проверяем, что POST действительно от B24 (access_token валиден
    # и соответствует user_id). В dev-режиме проверка пропускается.
    settings = get_settings()
    if not settings.dev_mode:
        access_token = auth_data.get("access_token", "")
        if not await _verify_b24_token(access_token, b24_user_id):
            return JSONResponse(
                {"error": "Недействительный B24 токен"}, status_code=403,
            )

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
