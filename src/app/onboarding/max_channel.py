"""MaxOnboardingChannel — MAX-онбординг в web-процессе (перенос admin_max).

Токен живёт в БД, конфликтов писателей нет — QR-флоу исполняется прямо в
web. Осознанные упрощения (личный инструмент, один web-процесс): состояние
логинов in-memory (рестарт web убивает незавершённый логин — безвредно,
токен ещё не в БД); 2FA-пароль живёт в памяти ровно до отправки 115;
токен пишется в БД и НИКОГДА не отдаётся API.
"""

import asyncio
import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.config import get_settings
from app.db import async_session
from app.messaging.max.factory import make_onboarding_client
from app.messaging.max.login import MaxQrLoginFlow, MaxSession
from app.messaging.max.protocol import build_user_agent
from app.models import AccountMember, Manager, Messenger, TgAccount, TgAccountStatus
from app.onboarding.types import LoginView, OnboardingStatus

logger = logging.getLogger(__name__)


@dataclass
class _MaxLoginState:
    flow: MaxQrLoginFlow
    task: asyncio.Task | None = None


def _profile_dto(account: TgAccount) -> dict:
    return {
        "id": account.id,
        "status": account.status.value,
        "name": account.display_name,
        "phone": account.phone,
        "messenger": Messenger.max.value,
    }


class MaxOnboardingChannel:
    messenger = Messenger.max

    def __init__(self, session_factory=None):
        self._session_factory = session_factory or async_session
        #: manager.id → состояние логина (in-memory; см. докстринг модуля).
        self._logins: dict[int, _MaxLoginState] = {}

    async def _find_account(self, manager_id: int) -> TgAccount | None:
        async with self._session_factory() as s:
            return (
                await s.execute(
                    select(TgAccount).where(
                        TgAccount.manager_id == manager_id,
                        TgAccount.messenger == Messenger.max,
                    )
                )
            ).scalar_one_or_none()

    async def account_view(self, manager_id: int) -> dict | None:
        account = await self._find_account(manager_id)
        return _profile_dto(account) if account is not None else None

    async def start(self, manager: Manager, *, force: bool = False) -> dict:
        account = await self._find_account(manager.id)
        if (
            account is not None
            and account.status == TgAccountStatus.active
            and not force
        ):
            return {"status": "already_active", "account": _profile_dto(account)}

        prev = self._logins.get(manager.id)
        if prev is not None and prev.task is not None and not prev.task.done():
            prev.task.cancel()

        settings = get_settings()
        flow = MaxQrLoginFlow(
            client=make_onboarding_client(),
            user_agent=build_user_agent(
                settings.max_app_version, settings.max_browser_ua
            ),
            deadline_sec=settings.max_onboarding_deadline_sec,
            password_timeout_sec=settings.max_onboarding_password_timeout_sec,
        )
        state = _MaxLoginState(flow=flow)
        state.task = asyncio.create_task(self._run_and_save(manager, flow))
        self._logins[manager.id] = state
        logger.info("MAX QR-логин запущен: manager_id=%s", manager.id)
        return {
            "status": OnboardingStatus.waiting.value,
            "qr_link": None,
            "detail": "qr_pending",
        }

    async def login_view(self, manager_id: int) -> LoginView | None:
        state = self._logins.get(manager_id)
        if state is None:
            return None
        return LoginView(
            status=OnboardingStatus(state.flow.status.value),
            qr_link=state.flow.qr_link,
            error=state.flow.error,
        )

    async def submit_password(self, manager_id: int, password: str) -> bool:
        state = self._logins.get(manager_id)
        if state is None:
            return False
        return state.flow.submit_password(password)

    async def cancel(self, manager_id: int) -> None:
        state = self._logins.pop(manager_id, None)
        if state is not None and state.task is not None and not state.task.done():
            state.task.cancel()

    async def _save_session(self, manager: Manager, session: MaxSession) -> TgAccount:
        """Upsert MAX-аккаунта менеджера со свежим токеном; status=active."""
        phone = session.phone or (
            f"MAX-{session.max_user_id}" if session.max_user_id else None
        )
        for attempt in range(2):
            async with self._session_factory() as s:
                existing = (
                    await s.execute(
                        select(TgAccount).where(
                            TgAccount.manager_id == manager.id,
                            TgAccount.messenger == Messenger.max,
                        )
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    existing.token = session.token
                    existing.device_id = session.device_id
                    existing.max_user_id = session.max_user_id
                    existing.display_name = session.name
                    if phone:
                        existing.phone = phone
                    existing.status = TgAccountStatus.active
                    await s.commit()
                    return existing
                acc = TgAccount(
                    messenger=Messenger.max,
                    phone=phone or f"MAX-mgr{manager.id}",
                    status=TgAccountStatus.active,
                    manager_id=manager.id,
                    token=session.token,
                    device_id=session.device_id,
                    max_user_id=session.max_user_id,
                    display_name=session.name,
                )
                s.add(acc)
                try:
                    await s.flush()
                    # Линия нового аккаунта: подключающий — первый участник.
                    s.add(AccountMember(account_id=acc.id, manager_id=manager.id))
                    await s.commit()
                    return acc
                except IntegrityError:
                    # Гонка вставки (двойной /start) — на второй итерации upsert.
                    await s.rollback()
                    if attempt == 1:
                        raise
        raise RuntimeError("unreachable")

    async def _run_and_save(self, manager: Manager, flow: MaxQrLoginFlow) -> None:
        await flow.run()
        if flow.result is None:
            return
        try:
            account = await self._save_session(manager, flow.result)
        except Exception:
            # Тихий успех без аккаунта в БД хуже ошибки: менеджер видел бы
            # «Готово», а bridge нечего подхватывать. Показываем ошибку.
            from app.messaging.max.login import QrFlowStatus

            flow.status = QrFlowStatus.error
            flow.error = "не удалось сохранить аккаунт — попробуйте ещё раз"
            logger.exception("MAX _save_session failed: manager_id=%s", manager.id)
            return
        logger.info(
            "MAX аккаунт сохранён: account_id=%s manager_id=%s",
            account.id, manager.id,
        )
