"""Spike (план 010): QR-онбординг менеджера через браузер.

Менеджер открывает /dev/qr, вводит свой b24_user_id и номер телефона,
сканирует QR приложением Telegram — сессия создаётся без SMS-кода.
Аккаунт в БД находится или создаётся на лету (логика из seed_manager.py),
.session пишется в тот же per-account путь, который ждёт SessionManager
bridge: <tg_sessions_dir>/account_<id>/session.

СПАЙК, НЕ ПРОДАКШЕН:
- маршрут доступен только при settings.dev_mode (иначе 404);
- состояние логинов — in-memory dict (теряется при рестарте web);
- после успешного скана нужен ручной рестарт bridge (docker compose restart
  bridge), чтобы он подхватил новую сессию;
- параллельные логины одного аккаунта не сериализуются по-настоящему
  (повторный /start отменяет предыдущую задачу).

Архитектурный контекст и выводы — docs/DESIGN-ADMIN-QR.md.
"""

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

from app.config import get_settings
from app.db import async_session
from app.models import Manager, ManagerRole, TgAccount, TgAccountStatus

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dev/qr", tags=["admin-qr"])

#: Сколько QR-итераций (wait → TimeoutError → recreate) делаем прежде чем
#: сдаться. Одна итерация живёт до expiry токена (~2-3 мин, задаёт сервер).
MAX_QR_ITERATIONS = 3

WAITING = "waiting"
AUTHORIZED = "authorized"
EXPIRED = "expired"
ERROR = "error"


@dataclass
class QrLoginState:
    """Состояние одного фонового QR-логина (in-memory, только для спайка)."""

    status: str = WAITING
    error: str | None = None
    qr: Any = None  # telethon QRLogin (или mock в тестах)
    client: Any = None
    task: asyncio.Task | None = None


# account_id → состояние. In-memory: при рестарте web теряется — для спайка ок
# (незавершённый логин просто придётся начать заново, .session не портится).
_logins: dict[int, QrLoginState] = {}


def _require_dev() -> None:
    """Gate всего маршрута: вне dev_mode — 404 (как placement dev-GET)."""
    if not get_settings().dev_mode:
        raise HTTPException(status_code=404, detail="Not Found")


async def _safe_disconnect(client: Any) -> None:
    """Disconnect, не роняя вызывающего кода (клиент мог уже отвалиться)."""
    try:
        await client.disconnect()
    except Exception:  # спайк: отключение best-effort
        logger.warning("disconnect failed (best-effort)", exc_info=True)


async def _find_or_create_account(b24_user_id: int, phone: str) -> TgAccount:
    """Найти или создать Manager+TgAccount (по образцу scripts/seed_manager.py).

    Manager ищется по b24_user_id; аккаунт — по manager_id (связь 1:1).
    Если аккаунт уже есть (например, заглушка из seed с +70000000000) —
    номер обновляется на переданный. Если номер занят чужим аккаунтом — 409.
    """
    async with async_session() as s:
        mgr = (
            await s.execute(
                select(Manager).where(Manager.b24_user_id == b24_user_id)
            )
        ).scalar_one_or_none()
        if mgr is None:
            mgr = Manager(
                name=f"Менеджер {b24_user_id}",
                b24_user_id=b24_user_id,
                role=ManagerRole.manager,
                is_active=True,
            )
            s.add(mgr)
            await s.flush()

        acc = (
            await s.execute(select(TgAccount).where(TgAccount.manager_id == mgr.id))
        ).scalar_one_or_none()
        if acc is not None:
            if acc.phone != phone:
                # «Переименовываем» заглушку/старый номер на реальный —
                # но номер мог быть занят чужим аккаунтом (phone unique):
                # без проверки был бы IntegrityError → 500 вместо 409.
                same_phone = (
                    await s.execute(select(TgAccount).where(TgAccount.phone == phone))
                ).scalar_one_or_none()
                if same_phone is not None and same_phone.id != acc.id:
                    raise HTTPException(
                        status_code=409,
                        detail=f"phone {phone} уже привязан к account_id={same_phone.id}",
                    )
                acc.phone = phone
        else:
            same_phone = (
                await s.execute(select(TgAccount).where(TgAccount.phone == phone))
            ).scalar_one_or_none()
            if same_phone is not None:
                raise HTTPException(
                    status_code=409,
                    detail=f"phone {phone} уже привязан к account_id={same_phone.id}",
                )
            settings = get_settings()
            # account.id ещё нет → создаём с flush, потом дописываем session_path.
            acc = TgAccount(
                phone=phone,
                session_path="",
                status=TgAccountStatus.offline,
                manager_id=mgr.id,
            )
            s.add(acc)
            await s.flush()
            acc.session_path = (
                f"{settings.tg_sessions_dir.rstrip('/')}/account_{acc.id}/session"
            )
        await s.commit()
        # expire_on_commit=False — объект usable после закрытия сессии.
        return acc


async def _mark_active(account_id: int) -> None:
    """Успешный QR-логин: переключить аккаунт в active (bridge видит это
    после своего рестарта — в спайке рестарт ручной)."""
    async with async_session() as s:
        acc = await s.get(TgAccount, account_id)
        if acc is not None:
            acc.status = TgAccountStatus.active
            await s.commit()


async def _run_qr_login(state: QrLoginState, account_id: int) -> None:
    """Фоновая корутина: ждать скана QR, на таймауте — новый QR (до 3 раз).

    wait() обязан исполняться, пока QR висит на экране (см. qrlogin.py:74);
    успешный скан авторизует сессию сразу, без кода подтверждения.
    """
    assert state.qr is not None and state.client is not None
    try:
        for attempt in range(MAX_QR_ITERATIONS):
            try:
                await state.qr.wait()
                await _mark_active(account_id)
                state.status = AUTHORIZED
                logger.info("QR login ok: account_id=%s", account_id)
                return
            except TimeoutError:
                # asyncio.TimeoutError == builtin TimeoutError (py3.11+);
                # qr_login.wait кидает его по истечении жизни токена.
                if attempt < MAX_QR_ITERATIONS - 1:
                    # Токен истёк — генерируем новый; qr.url меняется,
                    # /status отдаёт свежий url, фронт перерисует QR.
                    await state.qr.recreate()
                    logger.info(
                        "QR expired, recreated (attempt %s): account_id=%s",
                        attempt + 1, account_id,
                    )
                else:
                    state.status = EXPIRED
                    logger.warning(
                        "QR login expired after %s attempts: account_id=%s",
                        MAX_QR_ITERATIONS, account_id,
                    )
                    return
            except SessionPasswordNeededError:
                # 2FA cloud-пароль: wait() его не обходит (см. DESIGN-ADMIN-QR,
                # «Факты Telethon» №7). В спайке не поддержано — CLI auth.
                state.status = ERROR
                state.error = (
                    "У аккаунта включён 2FA-пароль: QR-флоу его не обходит. "
                    "Используйте CLI-логин (python -m app.main auth)."
                )
                return
    except Exception as exc:  # спайк: любое исключение уходит в state.error
        state.status = ERROR
        state.error = f"{type(exc).__name__}: {exc}"
        logger.exception("QR login failed: account_id=%s", account_id)
    finally:
        await _safe_disconnect(state.client)


@router.get("", response_model=None)
async def qr_page() -> HTMLResponse:
    """Страница QR-онбординга (static/qr.html). Только dev_mode."""
    _require_dev()
    settings = get_settings()
    html_path = Path(settings.static_dir) / "qr.html"
    if not html_path.is_file():
        raise HTTPException(status_code=500, detail="qr.html not found")
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


@router.get("/start", response_model=None)
async def qr_start(
    b24_user_id: int = Query(...),
    phone: str = Query(...),
) -> dict:
    """Запустить qr_login для (существующего или созданного) аккаунта.

    Возвращает {qr_url, account_id}. Логин-корутина живёт в фоне;
    статус: GET /dev/qr/status?account_id=... → waiting|authorized|expired|error.
    """
    _require_dev()
    phone = phone.strip()
    if not phone.startswith("+") or not phone[1:].isdigit():
        raise HTTPException(
            status_code=400, detail="phone нужен в международном формате, +7..."
        )

    settings = get_settings()
    account = await _find_or_create_account(b24_user_id, phone)

    # Повторный /start по тому же аккаунту: отменяем предыдущую попытку.
    prev = _logins.get(account.id)
    if prev is not None and prev.task is not None and not prev.task.done():
        prev.task.cancel()
        if prev.client is not None:
            await _safe_disconnect(prev.client)

    # Path-контракт: тот же per-account путь, что в auth.py / SessionManager.
    session_dir = Path(settings.tg_sessions_dir) / f"account_{account.id}"
    session_dir.mkdir(parents=True, exist_ok=True)

    client = TelegramClient(
        str(session_dir / "session"), settings.tg_api_id, settings.tg_api_hash
    )
    await client.connect()

    if await client.is_user_authorized():
        # Сессия уже валидна (повторный онбординг) — сразу активируем.
        await _mark_active(account.id)
        state = QrLoginState(status=AUTHORIZED, client=client)
        _logins[account.id] = state
        await _safe_disconnect(client)
        return {
            "account_id": account.id,
            "qr_url": None,
            "status": AUTHORIZED,
            "detail": "сессия уже авторизована",
        }

    qr = await client.qr_login()
    state = QrLoginState(status=WAITING, qr=qr, client=client)
    state.task = asyncio.create_task(_run_qr_login(state, account.id))
    _logins[account.id] = state
    logger.info(
        "QR login started: account_id=%s phone=%s b24_user_id=%s",
        account.id, phone, b24_user_id,
    )
    return {"account_id": account.id, "qr_url": qr.url, "status": WAITING}


@router.get("/status", response_model=None)
async def qr_status(account_id: int = Query(...)) -> dict:
    """Статус фонового QR-логина. qr_url актуален только в статусе waiting
    (после recreate() url меняется — фронт перерисовывает QR)."""
    _require_dev()
    state = _logins.get(account_id)
    if state is None:
        raise HTTPException(
            status_code=404,
            detail="нет активного QR-логина (web перезапущен или /start не звался?)",
        )
    qr_url = None
    if state.status == WAITING and state.qr is not None:
        qr_url = state.qr.url
    return {
        "account_id": account_id,
        "status": state.status,
        "qr_url": qr_url,
        "error": state.error,
    }
