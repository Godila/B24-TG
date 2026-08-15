"""Placement-обработчик Bitrix24: вкладка чата в карточке сделки (CRM_DEAL_DETAIL_TAB).

B24 открывает этот URL в iFrame и POST'ит form-data (плоские поля, см.
apidocs.bitrix24.ru «Что получает обработчик встройки»):
- query: DOMAIN, PROTOCOL, LANG, APP_SID
- тело: PLACEMENT, PLACEMENT_OPTIONS (JSON-строка {ID: <deal_id>}),
  AUTH_ID (access_token пользователя), AUTH_EXPIRES, REFRESH_ID, ...

user_id в запросе НЕТ: личность менеджера определяется вызовом user.current
по AUTH_ID (это же валидирует токен). Формат AUTH=JSON{user_id,...} —
из вебхука ONAPPINSTALL — здесь НЕ используется (баг, найденный первым
живым открытием вкладки).

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
from app.web.deps import ManagerDep
from app.web.session import create_session_cookie_params

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/placement", tags=["placement"])

_PLACEMENT_CODE = "CRM_DEAL_DETAIL_TAB"
_ADMIN_PLACEMENT_CODE = "LEFT_MENU"


async def _user_id_from_token(access_token: str) -> int | None:
    """Определить менеджера по AUTH_ID: user.current и валидирует токен.

    Placement-запрос не содержит user_id — токен выписан Битрикс24 конкретному
    пользователю, открывшему вкладку. Подделка исключена: без настоящего
    токена user.current не пройдёт.
    """
    if not access_token:
        return None
    settings = get_settings()
    client = Bitrix24Client(client_endpoint=settings.b24_portal.rstrip("/") + "/rest/")
    try:
        result = await client.call("user.current", auth_token=access_token)
    except Bitrix24Error:
        logger.warning("placement: invalid B24 access_token rejected")
        return None
    except Exception:
        logger.exception("placement: B24 token verification failed")
        return None
    finally:
        # Клиент одноразовый на placement-вход и держит свой httpx-пул —
        # обязательно закрываем, иначе утечка коннектов на каждый логин.
        await client.aclose()
    if not isinstance(result, dict):
        return None
    try:
        return int(result.get("ID", 0)) or None
    except (TypeError, ValueError):
        return None


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


@router.post("/admin", response_model=None)
async def placement_admin_post(
    placement: str = Form(default="", alias="PLACEMENT"),
    auth_id: str = Form(default="", alias="AUTH_ID"),
    auth: str = Form(default="", alias="AUTH"),
) -> HTMLResponse | JSONResponse:
    """Placement LEFT_MENU: админ-панель внутри интерфейса Битрикс24.

    Личность — как в /placement/deal (user.current по AUTH_ID; dev — из
    AUTH-JSON). Ставит сессионную куку и отдаёт admin.html — страницу
    подключения каналов (менеджеру) и supervisor-панель (администратору).
    """
    if placement != _ADMIN_PLACEMENT_CODE:
        return JSONResponse(
            {"error": f"unexpected placement: {placement!r}"}, status_code=400,
        )
    settings = get_settings()
    if settings.dev_mode:
        try:
            auth_data = json.loads(auth) if auth else {}
            b24_user_id = int(auth_data.get("user_id"))
        except (json.JSONDecodeError, TypeError, ValueError):
            return JSONResponse(
                {"error": "dev mode: requires AUTH.user_id"}, status_code=400,
            )
    else:
        b24_user_id = await _user_id_from_token(auth_id)
        if b24_user_id is None:
            return JSONResponse(
                {"error": "Недействительный B24 токен"}, status_code=403,
            )

    logger.info(
        "Placement opened: placement=%s b24_user_id=%s", placement, b24_user_id,
    )
    cookie_params = create_session_cookie_params(
        b24_user_id=b24_user_id, deal_id=None,
        secret=settings.session_secret, secure=not settings.dev_mode,
    )
    resp = HTMLResponse(content=_admin_html())
    resp.set_cookie(**cookie_params)
    return resp


@router.get("/admin", response_model=None)
async def placement_admin_get(_manager: ManagerDep) -> HTMLResponse:
    """GET-фолбэк (перезагрузка фрейма / прямая ссылка): страница отдаётся
    только при уже существующей сессионной куке — без неё пусть открывают
    через B24 (там поставится куку placement-вызовом)."""
    return HTMLResponse(content=_admin_html())


def _admin_html() -> str:
    """static/admin.html (админ-панель) с заглушкой при отсутствии."""
    settings = get_settings()
    html_path = Path(settings.static_dir) / "admin.html"
    if html_path.is_file():
        return html_path.read_text(encoding="utf-8")
    logger.warning("admin.html not found at %s — returning stub", html_path)
    return (
        '<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">'
        "<title>Bitrix-TG: каналы</title></head>"
        '<body><div>Панель недоступна: static/admin.html не найден.</div>'
        "</body></html>"
    )


@router.post("/deal", response_model=None)
async def placement_deal_post(
    placement: str = Form(default="", alias="PLACEMENT"),
    placement_options: str = Form(default="{}", alias="PLACEMENT_OPTIONS"),
    auth_id: str = Form(default="", alias="AUTH_ID"),
    auth: str = Form(default="", alias="AUTH"),
) -> HTMLResponse | JSONResponse:
    """Реальный placement-вызов от Bitrix24 (POST form-data).

    Прод: личность — user.current по AUTH_ID. Dev: user_id из legacy
    AUTH-JSON (локальные тесты виджета без реального B24).
    """
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

    settings = get_settings()
    if settings.dev_mode:
        # Dev: токена нет — берём user_id из AUTH-JSON (наш локальный формат).
        try:
            auth_data = json.loads(auth) if auth else {}
            b24_user_id = int(auth_data.get("user_id"))
        except (json.JSONDecodeError, TypeError, ValueError):
            return JSONResponse(
                {"error": "dev mode: requires AUTH.user_id"}, status_code=400,
            )
    else:
        b24_user_id = await _user_id_from_token(auth_id)
        if b24_user_id is None:
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
