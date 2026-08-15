"""Страница админ-панели + совместимые редиректы со старых URL."""

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

from app.config import get_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("", response_model=None)
async def admin_page() -> HTMLResponse:
    """Единая страница: карточки каналов (все роли) + панель (supervisor).

    Сам HTML без гейта (данные — только через /admin/api/* с авторизацией);
    открывается из B24 прямой вкладкой, сессионная кука ставится placement'
    сом или /dev/login.
    """
    settings = get_settings()
    html_path = Path(settings.static_dir) / "admin.html"
    if not html_path.is_file():
        raise HTTPException(status_code=500, detail="admin.html not found")
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


@router.get("/max", response_model=None)
async def admin_max_redirect() -> RedirectResponse:
    """Совместимость закладок: старая страница MAX-онбординга → /admin."""
    return RedirectResponse(url="/admin", status_code=307)
