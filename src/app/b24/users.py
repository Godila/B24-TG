"""Импорт сотрудников портала из B24 (user.get) в справочник менеджеров.

user.get уже исключает ботов, e-mail-пользователей и реплику; ACTIVE-фильтр
не передаём (форма фильтра капризна) — статус берём из поля каждой строки.
"""

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.b24.client import Bitrix24Client
from app.models import Manager, ManagerRole, TgAccount, TgAccountStatus

logger = logging.getLogger(__name__)

# Пагинация user.get: страница по умолчанию 50 строк.
PAGE_SIZE = 50


@dataclass(frozen=True, slots=True)
class B24User:
    b24_user_id: int
    name: str
    is_active: bool


def _parse_user(row: dict) -> B24User | None:
    """Fail-closed парсинг строки user.get: мусор (не dict / без числового
    ID / битые поля) — skip с логом, синк не должен падать от одной строки."""
    if not isinstance(row, dict):
        logger.warning("b24 user.get: строка не является объектом: %r", row)
        return None
    try:
        user_id = int(row.get("ID") or 0)
    except (TypeError, ValueError):
        user_id = 0
    if user_id <= 0:
        logger.warning("b24 user.get: строка без валидного ID пропущена: %r", row)
        return None
    # Грабля null-полей (см. crm._contact_display_name): NAME/LAST_NAME могут
    # прийти JSON-null — пустые части пропускаем.
    name = " ".join(p for p in (row.get("NAME"), row.get("LAST_NAME")) if p).strip()
    active_raw = row.get("ACTIVE", True)
    is_active = str(active_raw).lower() not in ("n", "false", "0")
    return B24User(
        b24_user_id=user_id,
        name=name or f"Сотрудник {user_id}",
        is_active=is_active,
    )


async def fetch_b24_users(client: Bitrix24Client, auth_token: str) -> list[B24User]:
    """Все пользователи портала с пагинацией start (throttle — в клиенте)."""
    users: list[B24User] = []
    start = 0
    while True:
        rows = await client.call("user.get", auth_token=auth_token, params={"start": start})
        if not rows:
            break
        users.extend(u for u in (_parse_user(r) for r in rows) if u is not None)
        if len(rows) < PAGE_SIZE:
            break
        start += len(rows)
    return users


async def managers_with_active_accounts(session: AsyncSession) -> set[int]:
    """manager_id с активными аккаунтами (legacy-привязка владельца; состав
    линий учитывается контракт-релизом — см. ponytail-маркер в tg_account)."""
    return set(
        (
            await session.execute(
                select(TgAccount.manager_id).where(
                    TgAccount.manager_id.is_not(None),
                    TgAccount.status == TgAccountStatus.active,
                )
            )
        )
        .scalars()
        .all()
    )


async def upsert_managers_from_b24(session_factory, users: list[B24User]) -> dict:
    """Сверка справочника менеджеров с сотрудниками B24.

    Новые — создаём (role=manager, is_active по B24); переименованных —
    обновляем; отсутствующих в B24 и деактивированных там — гасим
    (is_active=False), кроме тех, за кем активные аккаунты или роль
    последнего активного supervisor. Идемпотентна.
    """
    created = updated = deactivated = 0
    warnings: list[str] = []
    async with session_factory() as session:
        managers = (
            (await session.execute(select(Manager).order_by(Manager.id))).scalars().all()
        )
        by_b24 = {m.b24_user_id: m for m in managers}
        b24_ids = {u.b24_user_id for u in users}
        with_active_account = await managers_with_active_accounts(session)

        for u in users:
            m = by_b24.get(u.b24_user_id)
            if m is None:
                session.add(
                    Manager(
                        name=u.name,
                        b24_user_id=u.b24_user_id,
                        role=ManagerRole.manager,
                        is_active=u.is_active,
                    )
                )
                created += 1
                continue
            if m.name != u.name:
                m.name = u.name
                updated += 1
            if not u.is_active and m.is_active:
                if m.role == ManagerRole.supervisor and await is_last_active_supervisor(session, m):
                    warnings.append(
                        f"{m.name} (#{m.b24_user_id}): деактивирован в Битрикс24, "
                        "но он последний активный администратор"
                    )
                elif m.id in with_active_account:
                    warnings.append(
                        f"{m.name} (#{m.b24_user_id}): деактивирован в Битрикс24, "
                        "но за ним активные аккаунты — сначала отключите их"
                    )
                else:
                    m.is_active = False
                    deactivated += 1

        # Отсутствующие в user.get = удалённые с портала.
        for m in managers:
            if m.b24_user_id in b24_ids or not m.is_active:
                continue
            if m.role == ManagerRole.supervisor and await is_last_active_supervisor(session, m):
                warnings.append(
                    f"{m.name} (#{m.b24_user_id}) исчез из Битрикс24, "
                    "но он последний активный администратор"
                )
                continue
            if m.id in with_active_account:
                warnings.append(
                    f"{m.name} (#{m.b24_user_id}) исчез из Битрикс24, "
                    "но за ним активные аккаунты — сначала отключите их"
                )
                continue
            m.is_active = False
            deactivated += 1

        await session.commit()
    return {
        "created": created,
        "updated": updated,
        "deactivated": deactivated,
        "warnings": warnings,
    }


async def is_last_active_supervisor(session: AsyncSession, manager: Manager) -> bool:
    others = (
        (
            await session.execute(
                select(Manager.id).where(
                    Manager.role == ManagerRole.supervisor,
                    Manager.is_active.is_(True),
                    Manager.id != manager.id,
                )
            )
        )
        .scalars()
        .all()
    )
    return not others
