"""MaxOnboardingChannel — MAX-онбординг в web-процессе (перенос admin_max).

Субъект — линия (аккаунт): админ выдаёт share-ссылку, владелец телефона
сканирует QR. Токен живёт в БД, конфликтов писателей нет — QR-флоу
исполняется прямо в web. Осознанные упрощения (личный инструмент, один
web-процесс): состояние логинов in-memory (рестарт web убивает незавершённый
логин — безвредно, токен ещё не в БД; страница попросит новую ссылку);
2FA-пароль живёт в памяти ровно до отправки 115; токен пишется в БД и
НИКОГДА не отдаётся API.
"""

import asyncio
import logging
from dataclasses import dataclass

from app.config import get_settings
from app.db import async_session
from app.messaging.max.factory import make_onboarding_client
from app.messaging.max.login import MaxQrLoginFlow, MaxSession
from app.messaging.max.protocol import build_user_agent
from app.models import Messenger, TgAccount, TgAccountStatus
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
        #: account_id → состояние логина (in-memory; см. докстринг модуля).
        self._logins: dict[int, _MaxLoginState] = {}

    async def start(self, account: TgAccount, *, force: bool = False) -> dict:
        if (
            account is not None
            and account.status == TgAccountStatus.active
            and not force
        ):
            return {"status": "already_active", "account": _profile_dto(account)}

        prev = self._logins.get(account.id)
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
        state.task = asyncio.create_task(self._run_and_save(account, flow))
        self._logins[account.id] = state
        logger.info("MAX QR-логин запущен: account_id=%s", account.id)
        return {
            "status": OnboardingStatus.waiting.value,
            "qr_link": None,
            "detail": "qr_pending",
        }

    async def login_view(self, account_id: int) -> LoginView | None:
        state = self._logins.get(account_id)
        if state is None:
            return None
        return LoginView(
            status=OnboardingStatus(state.flow.status.value),
            qr_link=state.flow.qr_link,
            error=state.flow.error,
        )

    async def submit_password(self, account_id: int, password: str) -> bool:
        state = self._logins.get(account_id)
        if state is None:
            return False
        return state.flow.submit_password(password)

    async def cancel(self, account_id: int) -> None:
        state = self._logins.pop(account_id, None)
        if state is not None and state.task is not None and not state.task.done():
            state.task.cancel()

    async def _save_session(self, account: TgAccount, session: MaxSession) -> TgAccount:
        """Обновить креды линии свежим токеном; status=active."""
        phone = session.phone or (
            f"MAX-{session.max_user_id}" if session.max_user_id else None
        )
        async with self._session_factory() as s:
            existing = await s.get(TgAccount, account.id)
            if existing is None:
                raise RuntimeError(f"MAX: линия account_id={account.id} исчезла")
            existing.token = session.token
            existing.device_id = session.device_id
            existing.max_user_id = session.max_user_id
            existing.display_name = session.name
            if phone:
                existing.phone = phone
            existing.status = TgAccountStatus.active
            # У новой админской линии могло не быть ни одного участника —
            # не тот, кто «подключил» (владелец телефона мог быть без
            # доступа к ЧатМост); состав назначает админ в панели.
            await s.commit()
            return existing

    async def _run_and_save(self, account: TgAccount, flow: MaxQrLoginFlow) -> None:
        await flow.run()
        if flow.result is None:
            return
        try:
            saved = await self._save_session(account, flow.result)
        except Exception:
            # Тихий успех без аккаунта в БД хуже ошибки: сканировавший видел бы
            # «Готово», а bridge нечего подхватывать. Показываем ошибку.
            from app.messaging.max.login import QrFlowStatus

            flow.status = QrFlowStatus.error
            flow.error = "не удалось сохранить аккаунт — попробуйте ещё раз"
            logger.exception("MAX _save_session failed: account_id=%s", account.id)
            return
        logger.info("MAX аккаунт сохранён: account_id=%s", saved.id)
