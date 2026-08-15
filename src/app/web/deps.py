"""FastAPI-зависимости: аутентификация по сессионной куке + гейты."""

from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import Manager, ManagerRole
from app.web.session import SESSION_COOKIE

AsyncSessionDep = Annotated[AsyncSession, Depends(get_session)]


async def get_current_manager(request: Request, session: AsyncSessionDep) -> Manager:
    """Из запроса достаём сессионную куку -> b24_user_id -> Manager из БД.

    401 если кука отсутствует/невалидна или менеджер не найден/неактивен.
    """
    from app.config import get_settings

    settings = get_settings()
    token = request.cookies.get(SESSION_COOKIE)
    if token is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Не авторизован"
        )
    # Импорт здесь, чтобы избежать циклических импортов.
    from app.web.session import verify_session

    payload = verify_session(token, settings.session_secret)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Сессия истекла"
        )
    b24_user_id = payload.get("b24_user_id")
    if not isinstance(b24_user_id, int):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Невалидная сессия"
        )
    result = await session.execute(
        select(Manager).where(Manager.b24_user_id == b24_user_id)
    )
    manager = result.scalar_one_or_none()
    if manager is None or not manager.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Менеджер не найден или неактивен",
        )
    return manager


ManagerDep = Annotated[Manager, Depends(get_current_manager)]


async def get_current_supervisor(manager: ManagerDep) -> Manager:
    """Гейт админ-раздела: только роль supervisor (иначе 403)."""
    if manager.role != ManagerRole.supervisor:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Только для администратора"
        )
    return manager


SupervisorDep = Annotated[Manager, Depends(get_current_supervisor)]


def verify_origin(request: Request) -> None:
    """Минимальная CSRF-защита мутирующих cookie-роутов.

    Прод-кука SameSite=none (виджет живёт в iframe B24) прикрепляется и к
    кросс-сайтовым POST — без сверки Origin открыт был бы весь /admin и
    отправка сообщений из виджета. Origin отсутствует (same-origin/curl) —
    пропускаем; иначе он обязан совпадать с хостом запроса, порталом B24
    или CORS-списком настроек.
    """
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    origin = request.headers.get("origin")
    if not origin:
        return
    from app.config import get_settings

    settings = get_settings()
    host = request.headers.get("host", "")
    allowed = {
        f"https://{host}".rstrip("/"),
        f"http://{host}".rstrip("/"),
        f"https://{settings.b24_portal.rstrip('/')}",
        settings.b24_portal.rstrip("/"),
    }
    for extra in (settings.cors_origins or "").split(","):
        if extra.strip():
            allowed.add(extra.strip().rstrip("/"))
    if origin.rstrip("/") not in allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Origin не разрешён"
        )
