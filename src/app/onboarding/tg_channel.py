"""TgOnboardingChannel — TG-онбординг через команды в БД (вариант B).

Механика: web пишет строку login_commands (kind=qr_login), bridge
(LoginCommandWorker) исполняет QR-логин Telethon и пишет qr_link/статусы
обратно; web только читает. Инвариант «.session пишет только bridge»
сохранён, web не имеет доступа к session-volume.

Субъект — линия (аккаунт): админ выдаёт share-ссылку, телефон владельца
сканирует QR. Телефон НЕ спрашиваем: ``qr_login()`` он не нужен; номер/
имя bridge-backfill'ит из ``get_me()`` после авторизации.
"""

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update

from app.config import get_settings
from app.db import async_session
from app.models import (
    ACTIVE_STATUSES,
    LoginCommand,
    LoginCommandKind,
    LoginCommandStatus,
    Messenger,
    TgAccount,
    TgAccountStatus,
    terminate_active_commands,
)
from app.onboarding.types import LoginView, OnboardingStatus

logger = logging.getLogger(__name__)

#: Терминальные строки моложе этого окна ещё показываются фронту
#: (ошибка/истечение/успех), старее — login_view возвращает None.
_RECENT_TERMINAL_SEC = 600

_STATUS_TO_VIEW: dict[LoginCommandStatus, OnboardingStatus] = {
    LoginCommandStatus.pending: OnboardingStatus.waiting,
    LoginCommandStatus.waiting: OnboardingStatus.waiting,
    LoginCommandStatus.password_required: OnboardingStatus.password_required,
    LoginCommandStatus.authorized: OnboardingStatus.authorized,
    LoginCommandStatus.expired: OnboardingStatus.expired,
    LoginCommandStatus.error: OnboardingStatus.error,
}


def _profile_dto(account: TgAccount) -> dict:
    return {
        "id": account.id,
        "status": account.status.value,
        "name": account.display_name,
        "phone": account.phone,
        "messenger": Messenger.tg.value,
    }


class TgOnboardingChannel:
    messenger = Messenger.tg

    def __init__(self, session_factory=None):
        self._session_factory = session_factory or async_session

    async def _find_account(self, manager_id: int) -> TgAccount | None:
        async with self._session_factory() as s:
            return (
                await s.execute(
                    select(TgAccount).where(
                        TgAccount.manager_id == manager_id,
                        TgAccount.messenger == Messenger.tg,
                    )
                )
            ).scalar_one_or_none()

    async def account_view(self, manager_id: int) -> dict | None:
        account = await self._find_account(manager_id)
        return _profile_dto(account) if account is not None else None

    async def start(self, account: TgAccount, *, force: bool = False) -> dict:
        if (
            account is not None
            and account.status == TgAccountStatus.active
            and not force
        ):
            return {"status": "already_active", "account": _profile_dto(account)}

        deadline = datetime.now(UTC) + timedelta(
            seconds=get_settings().tg_onboarding_deadline_sec
        )
        async with self._session_factory() as s:
            # Терминализируем живые команды линии — и освобождаем
            # partial unique, и гасим идущий bridge-флоу (он увидит статус).
            await terminate_active_commands(
                s, account_id=account.id, messenger=Messenger.tg
            )
            s.add(
                LoginCommand(
                    manager_id=account.manager_id,
                    account_id=account.id,
                    messenger=Messenger.tg,
                    kind=LoginCommandKind.qr_login,
                    status=LoginCommandStatus.pending,
                    deadline_at=deadline,
                )
            )
            await s.commit()
        logger.info("TG QR-команда создана: account_id=%s", account.id)
        return {
            "status": OnboardingStatus.waiting.value,
            "qr_link": None,
            "detail": "qr_pending",
        }

    async def login_view(self, account_id: int) -> LoginView | None:
        async with self._session_factory() as s:
            cmd = (
                await s.execute(
                    select(LoginCommand)
                    .where(
                        LoginCommand.account_id == account_id,
                        LoginCommand.kind == LoginCommandKind.qr_login,
                    )
                    .order_by(LoginCommand.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
        if cmd is None:
            return None
        view_status = _STATUS_TO_VIEW.get(cmd.status)
        if view_status is None:
            # cancelled/done — фронт не показывает.
            return None
        if (
            cmd.status not in ACTIVE_STATUSES
            and cmd.created_at is not None
            and datetime.now(UTC).replace(tzinfo=None)
            - cmd.created_at.replace(tzinfo=None)
            > timedelta(seconds=_RECENT_TERMINAL_SEC)
        ):
            return None
        return LoginView(status=view_status, qr_link=cmd.qr_link, error=cmd.error)

    async def submit_password(self, account_id: int, password: str) -> bool:
        async with self._session_factory() as s:
            result = await s.execute(
                update(LoginCommand)
                .where(
                    LoginCommand.account_id == account_id,
                    LoginCommand.status == LoginCommandStatus.password_required,
                )
                .values(password_transit=password)
            )
            await s.commit()
            return result.rowcount > 0

    async def cancel(self, account_id: int) -> None:
        async with self._session_factory() as s:
            await terminate_active_commands(s, account_id=account_id)
            await s.commit()
