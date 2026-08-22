"""Webhook-обработчики Bitrix24: ONAPPINSTALL и события."""

import hmac
import logging

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy import select

from app.b24.client import Bitrix24Client
from app.b24.imbot import KEY_IMBOT_BOT_ID, ensure_bot_registered
from app.b24.token_manager import TokenManager
from app.config import get_settings
from app.db import async_session
from app.models import AppSetting
from app.web.schemas import OnAppInstallAuth

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhook/b24", tags=["bitrix24"])


def get_token_manager() -> TokenManager:
    """Factory для DI; в production поднимается с config. См. Task 10 wiring."""
    s = get_settings()
    return TokenManager(client_id=s.b24_client_id, client_secret=s.b24_client_secret)


async def _token_belongs_to_portal(auth: OnAppInstallAuth) -> bool:
    """Токен из payload валиден на endpoint из того же payload?

    B24 не умеет подписывать ONAPPINSTALL заголовками — реальный install-вызов
    с портала всегда без ``X-Webhook-Secret``. Подделка события бессмысленна:
    чужой/битый access_token не пройдёт user.current, а валидный токен можно
    получить только от самого портала.
    """
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                auth.client_endpoint.rstrip("/") + "/user.current",
                params={"auth": auth.access_token},
            )
        data = resp.json()
        return resp.status_code == 200 and isinstance(data.get("result"), dict)
    except (httpx.HTTPError, ValueError):
        # Сетевой сбой или не-JSON ответ: самопроверка не прошла — 401.
        logger.warning("ONAPPINSTALL: token self-check failed (network/invalid)")
        return False


async def _imbot_saved() -> bool:
    """Бот уже зарегистрирован на этом портале (app_settings)?"""
    async with async_session() as session:
        row = (
            await session.execute(
                select(AppSetting).where(AppSetting.key == KEY_IMBOT_BOT_ID)
            )
        ).scalar_one_or_none()
    return row is not None and (row.value or "").isdigit()


async def _save_imbot_id(bot_id: int) -> None:
    async with async_session() as session:
        row = (
            await session.execute(
                select(AppSetting).where(AppSetting.key == KEY_IMBOT_BOT_ID)
            )
        ).scalar_one_or_none()
        if row is None:
            session.add(AppSetting(key=KEY_IMBOT_BOT_ID, value=str(bot_id)))
        else:
            row.value = str(bot_id)
        await session.commit()


async def _ensure_imbot(auth: OnAppInstallAuth) -> None:
    """Право «Чат-боты» выдано, бот не зарегистрирован → поднять самому.

    Нулевое ручное сопровождение: установка с правом imbot сама создаёт
    бота и запоминает id в app_settings. Best-effort целиком — сбой не
    валит установку (токены уже сохранены): уведомления деградируют до
    LINK-фолбэка до следующей переустановки.
    """
    if "imbot" not in (auth.scope or "").split(","):
        return
    settings = get_settings()
    if not settings.public_base_url:
        logger.warning(
            "ONAPPINSTALL: imbot-право есть, но PUBLIC_BASE_URL пуст — бот не регистрируем"
        )
        return
    client = Bitrix24Client(
        client_endpoint=auth.client_endpoint, min_interval=settings.b24_min_call_interval
    )
    try:
        if await _imbot_saved():
            return  # уже зарегистрирован — REST не тратим (register идемпотентен)
        bot_id = await ensure_bot_registered(
            client,
            auth.access_token,
            webhook_url=settings.public_base_url.rstrip("/") + "/webhook/b24/imbot",
        )
        await _save_imbot_id(bot_id)
    except Exception:  # установка важнее бота
        logger.exception("ONAPPINSTALL: авто-регистрация бота не удалась")
    finally:
        await client.aclose()


@router.post("/onappinstall")
async def on_app_install(request: Request) -> JSONResponse:
    """Обработчик события установки приложения.

    Bitrix24 присылает OAuth-токены в POST body (поле auth). Реальный вызов
    с портала — form-urlencoded с php-массивами (auth[access_token]=… —
    живой лог 08-20), ручной/скриптовый — JSON; парсер общий с bizproc.

    Авторизация — один из двух эшелонов: ручной вызов несёт заголовок
    ``X-Webhook-Secret`` (``B24_WEBHOOK_SECRET`` из .env); реальный вызов
    с портала заголовка не имеет (B24 их не подписывает) и допускается
    самовалидацией токена через user.current (_token_belongs_to_portal).
    """
    # Приватный хелпер соседнего роута: JSON|form-парсинг с php-ключами —
    # единая точка разбора B24-тел (дублировать 30 строк дороже импорта).
    from app.web.routes.bizproc import _payload_dict

    payload = _payload_dict(await request.body(), request.headers.get("content-type", ""))
    if payload is None:
        # Битое тело → 422, не 500.
        logger.warning("ONAPPINSTALL: malformed body rejected (не JSON/form)")
        return JSONResponse({"error": "validation error"}, status_code=422)
    auth_raw = payload.get("auth", {}) if isinstance(payload, dict) else {}
    try:
        auth = OnAppInstallAuth.model_validate(auth_raw)
    except ValidationError as exc:
        # Только имена полей (missing/типы) — без значений: в auth лежат токены.
        problems = [
            f"{'.'.join(str(l) for l in e['loc'])}: {e['type']}" for e in exc.errors()
        ]
        logger.warning("ONAPPINSTALL: invalid auth payload rejected: %s", problems)
        return JSONResponse({"error": "validation error"}, status_code=422)

    settings = get_settings()
    secret = request.headers.get("X-Webhook-Secret", "")
    # Сравниваем байты: compare_digest(str, str) падает TypeError на не-ASCII
    # значениях заголовка (latin-1) — было бы 500 вместо 401.
    header_ok = bool(settings.b24_webhook_secret) and hmac.compare_digest(
        secret.encode("utf-8"), settings.b24_webhook_secret.encode("utf-8")
    )
    if not header_ok and not await _token_belongs_to_portal(auth):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    logger.info("ONAPPINSTALL received: member_id=%s", auth.member_id)

    tm = get_token_manager()
    await tm.save_install_data(auth.model_dump())
    await _ensure_imbot(auth)

    return JSONResponse({"status": "ok"})
