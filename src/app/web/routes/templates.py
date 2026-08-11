"""API шаблонов сообщений для быстрых ответов в Web UI."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import Manager, Template
from app.web.deps import get_current_manager
from app.web.schemas import TemplateOut

router = APIRouter(prefix="/api", tags=["templates"])

ManagerDep = Annotated[Manager, Depends(get_current_manager)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]


@router.get("/templates")
async def list_templates(
    manager: ManagerDep,
    session: SessionDep,
    category: str | None = Query(default=None),
) -> list[TemplateOut]:
    """Список шаблонов. Опциональный фильтр по category."""
    stmt = select(Template).order_by(
        Template.category.asc().nullslast(), Template.title.asc()
    )
    if category is not None:
        stmt = stmt.where(Template.category == category)
    result = await session.execute(stmt)
    return [
        TemplateOut(
            id=t.id, title=t.title, body=t.body, category=t.category,
        )
        for t in result.scalars().all()
    ]
