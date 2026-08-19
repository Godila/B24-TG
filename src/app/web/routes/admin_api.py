"""/admin/api — единый API онбординга и панели администратора.

Онбординг канал-агностичен: ``/admin/api/onboarding/{channel}/...`` через
контракт OnboardingChannel (механика MAX — web-процесс, TG — команды в
БД → bridge). Панель (менеджеры/аккаунты) — за SupervisorDep. Все POST
прикрыты verify_origin (прод-кука SameSite=none — см. deps).
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from app.b24.client import Bitrix24Client, Bitrix24Error
from app.b24.sync import TIMELINE_MODES
from app.b24.token_manager import TokenManager
from app.b24.users import fetch_b24_users, is_last_active_supervisor, upsert_managers_from_b24
from app.db import async_session
from app.models import (
    AccountMember,
    ConnectToken,
    Dialog,
    LineRole,
    LoginCommand,
    LoginCommandKind,
    LoginCommandStatus,
    Manager,
    ManagerRole,
    Messenger,
    TgAccount,
    TgAccountStatus,
    issue_connect_token,
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


class ManagerCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    b24_user_id: int


class ManagerPatchIn(BaseModel):
    name: str | None = None
    role: ManagerRole | None = None
    is_active: bool | None = None
    is_readonly: bool | None = None


class LineMemberIn(BaseModel):
    manager_id: int
    role: LineRole = LineRole.participant


class LineMemberPatchIn(BaseModel):
    role: LineRole


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


# ---------------------------------------------------------------------- #
# Панель администратора (SupervisorDep)
# ---------------------------------------------------------------------- #
class SettingsIn(BaseModel):
    # Список режимов — единственный источник истины TIMELINE_MODES в
    # b24/sync.py; regex собираем из него, чтобы не дублировать руками.
    # Оба поля опциональны: PUT применяет только переданные (панель шлёт
    # тот контрол, который меняли).
    timeline_mode: str | None = Field(default=None, pattern="^(" + "|".join(TIMELINE_MODES) + ")$")
    media_to_timeline: bool | None = None


@router.get("/settings", response_model=None)
async def get_settings(supervisor: SupervisorDep) -> dict:
    """Глобальные настройки приложения (для supervisor-панели)."""
    from app.bridge.crm_sync_repo import get_media_to_timeline, get_timeline_mode

    return {
        "timeline_mode": await get_timeline_mode(async_session),
        "media_to_timeline": await get_media_to_timeline(async_session),
    }


@router.put("/settings", response_model=None)
async def put_settings(body: SettingsIn, supervisor: SupervisorDep) -> dict:
    from app.bridge.crm_sync_repo import set_media_to_timeline, set_timeline_mode

    if body.timeline_mode is None and body.media_to_timeline is None:
        raise HTTPException(status_code=422, detail="Нечего обновлять")
    if body.timeline_mode is not None:
        await set_timeline_mode(async_session, body.timeline_mode)
    if body.media_to_timeline is not None:
        await set_media_to_timeline(async_session, body.media_to_timeline)
    return {
        "timeline_mode": body.timeline_mode,
        "media_to_timeline": body.media_to_timeline,
    }


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
        managers = (await s.execute(select(Manager).order_by(Manager.id))).scalars().all()
        accounts = (
            (await s.execute(select(TgAccount).order_by(TgAccount.manager_id, TgAccount.id)))
            .scalars()
            .all()
        )
    by_manager: dict[int, list[TgAccount]] = {}
    for a in accounts:
        by_manager.setdefault(a.manager_id, []).append(a)
    return [_manager_dto(m, by_manager.get(m.id, [])) for m in managers]


@router.post("/managers/sync_b24", response_model=None)
async def sync_managers_b24(supervisor: SupervisorDep) -> dict:
    """«Обновить из CRM»: user.get → upsert справочника менеджеров."""
    # Локальный импорт: глобальное имя get_settings в этом модуле занято
    # роутом настроек таймлайна ниже.
    from app.config import get_settings

    settings = get_settings()
    token = await TokenManager(
        client_id=settings.b24_client_id,
        client_secret=settings.b24_client_secret,
    ).get_token()
    if token is None:
        raise HTTPException(
            status_code=503, detail="Приложение ЧатМост не установлено в Битрикс24"
        )
    # Одноразовый клиент на запрос (web-процесс; паттерн placement.py).
    client = Bitrix24Client(
        client_endpoint=token.client_endpoint or settings.b24_portal.rstrip("/") + "/rest/",
        min_interval=settings.b24_min_call_interval,
    )
    try:
        users = await fetch_b24_users(client, token.access_token)
    except Bitrix24Error as e:
        if e.code == "ERROR_SCOPE":
            raise HTTPException(
                status_code=400,
                detail="У приложения нет права «Пользователи» — "
                "переустановите приложение с этим разрешением",
            ) from None
        raise HTTPException(status_code=502, detail=f"Битрикс24: {e}") from None
    finally:
        await client.aclose()
    result = await upsert_managers_from_b24(async_session, users)
    logger.info(
        "Синк менеджеров из B24: created=%s updated=%s deactivated=%s (by supervisor %s)",
        result["created"],
        result["updated"],
        result["deactivated"],
        supervisor.id,
    )
    return result


# ---------------------------------------------------------------------- #
# Линии (аккаунты) и их участники
# ---------------------------------------------------------------------- #
class LineCreateIn(BaseModel):
    messenger: Messenger


def _member_dto(am: AccountMember, m: Manager) -> dict:
    return {
        "manager_id": m.id,
        "name": m.name,
        "b24_user_id": m.b24_user_id,
        "role": am.role.value,
    }


@router.get("/lines", response_model=None)
async def list_lines(supervisor: SupervisorDep) -> list[dict]:
    async with async_session() as s:
        accounts = (
            (await s.execute(select(TgAccount).order_by(TgAccount.id))).scalars().all()
        )
        rows = (
            await s.execute(
                select(AccountMember, Manager)
                .join(Manager, AccountMember.manager_id == Manager.id)
                .order_by(AccountMember.account_id, AccountMember.id)
            )
        ).all()
    by_account: dict[int, list[dict]] = {}
    for am, m in rows:
        by_account.setdefault(am.account_id, []).append(_member_dto(am, m))
    return [
        {
            "id": a.id,
            "messenger": a.messenger.value,
            "phone": a.phone,
            "name": a.display_name,
            "status": a.status.value,
            "members": by_account.get(a.id, []),
        }
        for a in accounts
    ]


async def _load_member(
    s, account_id: int, manager_id: int
) -> AccountMember:
    return (
        await s.execute(
            select(AccountMember).where(
                AccountMember.account_id == account_id,
                AccountMember.manager_id == manager_id,
            )
        )
    ).scalar_one_or_none()


@router.post("/lines/{account_id}/members", status_code=201, response_model=None)
async def add_line_member(
    account_id: int, body: LineMemberIn, supervisor: SupervisorDep
) -> dict:
    async with async_session() as s:
        account = await s.get(TgAccount, account_id)
        if account is None:
            raise HTTPException(status_code=404, detail="линия не найдена")
        m = await s.get(Manager, body.manager_id)
        if m is None or not m.is_active:
            raise HTTPException(
                status_code=422, detail="менеджер не найден или неактивен"
            )
        member = AccountMember(
            account_id=account_id, manager_id=body.manager_id, role=body.role
        )
        s.add(member)
        try:
            await s.commit()
        except IntegrityError:
            raise HTTPException(status_code=409, detail="уже участник линии") from None
        logger.info(
            "Участник линии добавлен: account_id=%s manager_id=%s role=%s (by supervisor %s)",
            account_id, body.manager_id, body.role.value, supervisor.id,
        )
        return _member_dto(member, m)


@router.patch("/lines/{account_id}/members/{manager_id}", response_model=None)
async def patch_line_member(
    account_id: int, manager_id: int, body: LineMemberPatchIn, supervisor: SupervisorDep
) -> dict:
    async with async_session() as s:
        member = await _load_member(s, account_id, manager_id)
        if member is None:
            raise HTTPException(status_code=404, detail="участник не найден")
        member.role = body.role
        await s.commit()
        m = await s.get(Manager, manager_id)
        logger.info(
            "Роль участника линии: account_id=%s manager_id=%s role=%s (by supervisor %s)",
            account_id, manager_id, body.role.value, supervisor.id,
        )
        return _member_dto(member, m)


@router.delete("/lines/{account_id}/members/{manager_id}", response_model=None)
async def remove_line_member(
    account_id: int, manager_id: int, supervisor: SupervisorDep
) -> dict:
    async with async_session() as s:
        member = await _load_member(s, account_id, manager_id)
        if member is None:
            raise HTTPException(status_code=404, detail="участник не найден")
        await s.delete(member)
        # Ответственный из состава линии сбрасывается: диалоги линии
        # становятся «без ответственного», а не висят на невидимом им
        # бывшем участнике.
        await s.execute(
            update(Dialog)
            .where(
                Dialog.account_id == account_id,
                Dialog.assigned_user_id == manager_id,
            )
            .values(assigned_user_id=None)
        )
        await s.commit()
        logger.info(
            "Участник линии удалён: account_id=%s manager_id=%s (by supervisor %s)",
            account_id, manager_id, supervisor.id,
        )
        return {"status": "removed"}


@router.post("/lines", status_code=201, response_model=None)
async def create_line(body: LineCreateIn, supervisor: SupervisorDep) -> dict:
    """Заготовка линии: офлайн-аккаунт без участников. Номер «оживает»
    подключением по share-ссылке (connect), состав назначается после."""
    async with async_session() as s:
        account = TgAccount(
            messenger=body.messenger,
            phone=f"{body.messenger.value.upper()}-line",
            status=TgAccountStatus.offline,
        )
        s.add(account)
        await s.commit()
        logger.info(
            "Линия создана: account_id=%s messenger=%s (by supervisor %s)",
            account.id, body.messenger.value, supervisor.id,
        )
        return {
            "id": account.id,
            "messenger": account.messenger.value,
            "phone": account.phone,
            "name": None,
            "status": account.status.value,
            "members": [],
        }


@router.post("/lines/{account_id}/connect/{channel}", response_model=None)
async def line_connect(
    account_id: int, channel: Messenger, supervisor: SupervisorDep, *, force: bool = False
) -> dict:
    """Запустить QR-логин линии и выдать share-ссылку владельцу номера."""
    async with async_session() as s:
        account = await s.get(TgAccount, account_id)
        if account is None:
            raise HTTPException(status_code=404, detail="линия не найдена")
        if account.messenger != channel:
            raise HTTPException(
                status_code=409, detail=f"линия канала {account.messenger.value}"
            )
        from app.config import get_settings as load_settings

        token = await issue_connect_token(
            s,
            account=account,
            created_by=supervisor.id,
            ttl_sec=load_settings().connect_token_ttl_sec,
        )
        await s.commit()
    data = await _channel(channel).start(account, force=force)
    logger.info(
        "Share-ссылка выдана: account_id=%s channel=%s (by supervisor %s)",
        account_id, channel.value, supervisor.id,
    )
    return {
        **data,
        "share_url": f"/connect/{token.raw_token}",
        "expires_at": token.expires_at,
    }


@router.post("/lines/{account_id}/connect/{channel}/cancel", response_model=None)
async def line_connect_cancel(
    account_id: int, channel: Messenger, supervisor: SupervisorDep
) -> dict:
    """Отменить подключение: живой логин терминализируется, ссылка гасится."""
    async with async_session() as s:
        await terminate_active_commands(s, account_id=account_id, messenger=channel)
        await s.execute(
            update(ConnectToken)
            .where(
                ConnectToken.account_id == account_id,
                ConnectToken.used_at.is_(None),
                ConnectToken.revoked_at.is_(None),
            )
            .values(revoked_at=func.now())
        )
        await s.commit()
    logger.info(
        "Подключение линии отменено: account_id=%s (by supervisor %s)",
        account_id, supervisor.id,
    )
    return {"status": "cancelled"}


@router.post("/managers", status_code=201, response_model=None)
async def create_manager(body: ManagerCreateIn, supervisor: SupervisorDep) -> dict:
    async with async_session() as s:
        existing = (
            await s.execute(select(Manager).where(Manager.b24_user_id == body.b24_user_id))
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
            m.id,
            body.b24_user_id,
            supervisor.id,
        )
        return _manager_dto(m, [])


@router.patch("/managers/{manager_id}", response_model=None)
async def patch_manager(manager_id: int, body: ManagerPatchIn, supervisor: SupervisorDep) -> dict:
    async with async_session() as s:
        m = await s.get(Manager, manager_id)
        if m is None:
            raise HTTPException(status_code=404, detail="менеджер не найден")
        if body.is_active is False:
            # Деактивация = запрет входа; активные аккаунты продолжили бы
            # переписку — честнее потребовать сначала отвязать их.
            active_accounts = (
                (
                    await s.execute(
                        select(TgAccount).where(
                            TgAccount.manager_id == m.id,
                            TgAccount.status == TgAccountStatus.active,
                        )
                    )
                )
                .scalars()
                .all()
            )
            if active_accounts:
                channels = ", ".join(a.messenger.value.upper() for a in active_accounts)
                raise HTTPException(
                    status_code=409,
                    detail=f"сначала отключите активные аккаунты ({channels})",
                )
        if (
            body.role is not None
            and body.role != m.role
            and m.role == ManagerRole.supervisor
            and await is_last_active_supervisor(s, m)
        ):
            raise HTTPException(
                status_code=409,
                detail="нельзя снять роль администратора с последнего активного администратора",
            )
        if body.name is not None:
            m.name = body.name
        if body.role is not None:
            m.role = body.role
        if body.is_active is not None:
            m.is_active = body.is_active
        if body.is_readonly is not None:
            m.is_readonly = body.is_readonly
        await s.commit()
        accounts = (
            (await s.execute(select(TgAccount).where(TgAccount.manager_id == m.id))).scalars().all()
        )
        logger.info(
            "Менеджер изменён: id=%s patch=%s (by supervisor %s)",
            m.id,
            body.model_dump(exclude_none=True),
            supervisor.id,
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
                account.id,
                supervisor.id,
            )
            return {"status": "logout_scheduled"}
        account.status = TgAccountStatus.offline
        account.token = None
        account.device_id = None
        await s.commit()
        logger.info(
            "MAX аккаунт деактивирован: account_id=%s (by supervisor %s)",
            account.id,
            supervisor.id,
        )
        return {"status": "deactivated"}
