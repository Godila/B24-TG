"""API шаблонов сообщений для быстрых ответов в Web UI.

Чтение — любой менеджер (панель «Шаблоны» в композере), мутации — только
supervisor (админ-панель). verify_origin закрывает кросс-сайтовые POST
(прод-кука SameSite=none — см. deps).
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import Manager, Template
from app.web.deps import SupervisorDep, get_current_manager, verify_origin
from app.web.schemas import TemplateCreateIn, TemplateOut

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api", tags=["templates"], dependencies=[Depends(verify_origin)]
)

ManagerDep = Annotated[Manager, Depends(get_current_manager)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]


def _dto(t: Template) -> TemplateOut:
    return TemplateOut(id=t.id, title=t.title, body=t.body, category=t.category)


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
    return [_dto(t) for t in result.scalars().all()]


@router.post("/templates", status_code=201)
async def create_template(
    body: TemplateCreateIn, supervisor: SupervisorDep, session: SessionDep
) -> TemplateOut:
    tpl = Template(
        title=body.title,
        body=body.body,
        category=body.category,
        created_by=supervisor.id,
    )
    session.add(tpl)
    await session.commit()
    logger.info("Шаблон создан: id=%s (supervisor_id=%s)", tpl.id, supervisor.id)
    return _dto(tpl)


@router.put("/templates/{template_id}")
async def update_template(
    template_id: int,
    body: TemplateCreateIn,
    supervisor: SupervisorDep,
    session: SessionDep,
) -> TemplateOut:
    tpl = await session.get(Template, template_id)
    if tpl is None:
        raise HTTPException(status_code=404, detail="Шаблон не найден")
    tpl.title = body.title
    tpl.body = body.body
    tpl.category = body.category
    await session.commit()
    return _dto(tpl)


@router.delete("/templates/{template_id}")
async def delete_template(
    template_id: int, supervisor: SupervisorDep, session: SessionDep
) -> dict:
    tpl = await session.get(Template, template_id)
    if tpl is None:
        raise HTTPException(status_code=404, detail="Шаблон не найден")
    await session.delete(tpl)
    await session.commit()
    logger.info("Шаблон удалён: id=%s (supervisor_id=%s)", template_id, supervisor.id)
    return {"status": "removed"}
