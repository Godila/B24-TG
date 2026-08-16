"""/admin/api — единый API онбординга и панели администратора.

Онбординг канал-агностичен: ``/admin/api/onboarding/{channel}/...`` через
контракт OnboardingChannel (механика MAX — web-процесс, TG — команды в
БД → bridge). Панель (менеджеры/аккаунты) — за SupervisorDep. Все POST
прикрыты verify_origin (прод-кука SameSite=none — см. deps).
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.b24.sync import TIMELINE_MODES
from app.db import async_session
from app.models import (
    LoginCommand,
    LoginCommandKind,
    LoginCommandStatus,
    Manager,
    ManagerRole,
    Messenger,
    TgAccount,
    TgAccountStatus,
    terminate_active_commands,
)
from app.onboarding.protocol import OnboardingChannel
from app.web.deps import ManagerDep, SupervisorDep, verify_origin

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/admin/api",
    tags=["admin-api"],
    dependencies=[Depends(verify_origin)],
)

#: Каналы онбординга; собираются в create_app (см. register_channels).
_channels: dict[Messenger, OnboardingChannel] = {}


def register_channels(channels: dict[Messenger, OnboardingChannel]) -> None:
    _channels.clear()
    _channels.update(channels)


def _channel(messenger: Messenger) -> OnboardingChannel:
    channel = _channels.get(messenger)
    if channel is None:
        raise HTTPException(status_code=404, detail=f"канал {messenger} не поддерживается")
    return channel


class PasswordIn(BaseModel):
    password: str


class ManagerCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    b24_user_id: int


class ManagerPatchIn(BaseModel):
    name: str | None = None
    is_active: bool | None = None
    is_readonly: bool | None = None


# ---------------------------------------------------------------------- #
# Мой профиль + онбординг (ManagerDep — self-service)
# ---------------------------------------------------------------------- #
@router.get("/me", response_model=None)
async def me(manager: ManagerDep) -> dict:
    accounts = []
    for channel in _channels.values():
        view = await channel.account_view(manager.id)
        if view is not None:
            accounts.append(view)
    return {
        "id": manager.id,
        "name": manager.name,
        "b24_user_id": manager.b24_user_id,
        "role": manager.role.value,
        "is_readonly": manager.is_readonly,
        "accounts": accounts,
    }


@router.post("/onboarding/{channel}/start", response_model=None)
async def onboarding_start(
    channel: Messenger, manager: ManagerDep, *, force: bool = False
) -> dict:
    return await _channel(channel).start(manager, force=force)


@router.get("/onboarding/{channel}/status", response_model=None)
async def onboarding_status(channel: Messenger, manager: ManagerDep) -> dict:
    view = await _channel(channel).login_view(manager.id)
    if view is None:
        raise HTTPException(status_code=404, detail="нет активного логина")
    return view.as_dict()


@router.post("/onboarding/{channel}/password", response_model=None)
async def onboarding_password(
    channel: Messenger, body: PasswordIn, manager: ManagerDep
) -> dict:
    if not await _channel(channel).submit_password(manager.id, body.password):
        raise HTTPException(
            status_code=409, detail="логин не ждёт пароль (статус не password_required)"
        )
    return {"status": "submitted"}


@router.post("/onboarding/{channel}/cancel", response_model=None)
async def onboarding_cancel(channel: Messenger, manager: ManagerDep) -> dict:
    await _channel(channel).cancel(manager.id)
    return {"status": "cancelled"}


# ---------------------------------------------------------------------- #
# Панель администратора (SupervisorDep)
# ---------------------------------------------------------------------- #
class SettingsIn(BaseModel):
    # Список режимов — единственный источник истины TIMELINE_MODES в
    # b24/sync.py; regex собираем из него, чтобы не дублировать руками.
    timeline_mode: str = Field(
        pattern="^(" + "|".join(TIMELINE_MODES) + ")$"
    )


@router.get("/settings", response_model=None)
async def get_settings(supervisor: SupervisorDep) -> dict:
    """Глобальные настройки приложения (для supervisor-панели)."""
    from app.bridge.crm_sync_repo import get_timeline_mode

    mode = await get_timeline_mode(async_session)
    return {"timeline_mode": mode}


@router.put("/settings", response_model=None)
async def put_settings(
    body: SettingsIn, supervisor: SupervisorDep
) -> dict:
    from app.bridge.crm_sync_repo import set_timeline_mode

    await set_timeline_mode(async_session, body.timeline_mode)
    return {"timeline_mode": body.timeline_mode}


def _manager_dto(m: Manager, accounts: list[TgAccount]) -> dict:
    return {
        "id": m.id,
        "name": m.name,
        "b24_user_id": m.b24_user_id,
        "role": m.role.value,
        "is_active": m.is_active,
        "is_readonly": m.is_readonly,
        "accounts": [
            {
                "id": a.id,
                "messenger": a.messenger.value,
                "status": a.status.value,
                "phone": a.phone,
                "name": a.display_name,
            }
            for a in accounts
        ],
    }


@router.get("/managers", response_model=None)
async def list_managers(supervisor: SupervisorDep) -> list[dict]:
    async with async_session() as s:
        managers = (
            (await s.execute(select(Manager).order_by(Manager.id))).scalars().all()
        )
        accounts = (
            (
                await s.execute(
                    select(TgAccount).order_by(TgAccount.manager_id, TgAccount.id)
                )
            )
            .scalars()
            .all()
        )
    by_manager: dict[int, list[TgAccount]] = {}
    for a in accounts:
        by_manager.setdefault(a.manager_id, []).append(a)
    return [_manager_dto(m, by_manager.get(m.id, [])) for m in managers]


@router.post("/managers", status_code=201, response_model=None)
async def create_manager(
    body: ManagerCreateIn, supervisor: SupervisorDep
) -> dict:
    async with async_session() as s:
        existing = (
            await s.execute(
                select(Manager).where(Manager.b24_user_id == body.b24_user_id)
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise HTTPException(
                status_code=409,
                detail=f"менеджер с b24_user_id={body.b24_user_id} уже существует",
            )
        m = Manager(
            name=body.name,
            b24_user_id=body.b24_user_id,
            role=ManagerRole.manager,
            is_active=True,
        )
        s.add(m)
        try:
            await s.commit()
        except IntegrityError:
            raise HTTPException(status_code=409, detail="дубликат b24_user_id") from None
        logger.info(
            "Менеджер создан: id=%s b24_user_id=%s (by supervisor %s)",
            m.id, body.b24_user_id, supervisor.id,
        )
        return _manager_dto(m, [])


@router.patch("/managers/{manager_id}", response_model=None)
async def patch_manager(
    manager_id: int, body: ManagerPatchIn, supervisor: SupervisorDep
) -> dict:
    async with async_session() as s:
        m = await s.get(Manager, manager_id)
        if m is None:
            raise HTTPException(status_code=404, detail="менеджер не найден")
        if body.is_active is False:
            # Деактивация = запрет входа; активные аккаунты продолжили бы
            # переписку — честнее потребовать сначала отвязать их.
            active_accounts = (
                await s.execute(
                    select(TgAccount).where(
                        TgAccount.manager_id == m.id,
                        TgAccount.status == TgAccountStatus.active,
                    )
                )
            ).scalars().all()
            if active_accounts:
                channels = ", ".join(a.messenger.value.upper() for a in active_accounts)
                raise HTTPException(
                    status_code=409,
                    detail=f"сначала отключите активные аккаунты ({channels})",
                )
        if body.name is not None:
            m.name = body.name
        if body.is_active is not None:
            m.is_active = body.is_active
        if body.is_readonly is not None:
            m.is_readonly = body.is_readonly
        await s.commit()
        accounts = (
            (
                await s.execute(
                    select(TgAccount).where(TgAccount.manager_id == m.id)
                )
            )
            .scalars()
            .all()
        )
        logger.info(
            "Менеджер изменён: id=%s patch=%s (by supervisor %s)",
            m.id, body.model_dump(exclude_none=True), supervisor.id,
        )
        return _manager_dto(m, list(accounts))


@router.post("/accounts/{account_id}/unlink", status_code=202, response_model=None)
async def unlink_account(account_id: int, supervisor: SupervisorDep) -> dict:
    """Отвязка аккаунта: TG — bridge исполнит log_out по команде; MAX —
    локальная деактивация (токен стирается, AccountSync снимет провайдера)."""
    async with async_session() as s:
        account = await s.get(TgAccount, account_id)
        if account is None:
            raise HTTPException(status_code=404, detail="аккаунт не найден")
        if account.messenger == Messenger.tg:
            # Гасим живые логин-команды аккаунта и ставим log_out (одна txn —
            # uq_login_commands_active требует свободной пары).
            await terminate_active_commands(s, account_id=account.id)
            s.add(
                LoginCommand(
                    manager_id=account.manager_id,
                    account_id=account.id,
                    messenger=Messenger.tg,
                    kind=LoginCommandKind.log_out,
                    status=LoginCommandStatus.pending,
                )
            )
            await s.commit()
            logger.info(
                "TG отвязка запланирована: account_id=%s (by supervisor %s)",
                account.id, supervisor.id,
            )
            return {"status": "logout_scheduled"}
        account.status = TgAccountStatus.offline
        account.token = None
        account.device_id = None
        await s.commit()
        logger.info(
            "MAX аккаунт деактивирован: account_id=%s (by supervisor %s)",
            account.id, supervisor.id,
        )
        return {"status": "deactivated"}
