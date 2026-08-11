"""FastAPI-зависимости: аутентификация по сессионной куке."""

from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import Manager
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
