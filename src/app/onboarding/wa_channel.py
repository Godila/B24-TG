"""WaOnboardingChannel — WhatsApp-онбординг в web-процессе (паттерн MAX).

Сессию создаёт и держит OpenWA-сайдкар (QR генерит там же, включая прокси
egress); web рулит жизненным циклом по REST и опрашивает QR/статус. Креды
линии — ``wa_session_id`` (токен-колонки канал не использует). QR у WA —
PNG data-URL (connect-страница рендерит его <img>-веткой). Осознанные
упрощения (личный инструмент, один web-процесс): состояние логинов
in-memory — рестарт web убивает незавершённый логин; забытая qr_ready-
сессия в сайдкаре дочистится по дедлайну задачи либо руками из дашборда.
"""

import asyncio
import logging
import time
from dataclasses import dataclass

from app.config import get_settings
from app.db import async_session
from app.messaging.whatsapp.api import OpenWaClient, WaError, wa_logout_and_delete
from app.models import Messenger, TgAccount, TgAccountStatus
from app.onboarding.types import LoginView, OnboardingStatus

logger = logging.getLogger(__name__)

_QR_POLL_SEC = 2.0


@dataclass
class _WaLoginState:
    session_id: str
    task: asyncio.Task | None = None
    status: OnboardingStatus = OnboardingStatus.waiting
    qr_link: str | None = None
    error: str | None = None


class WaOnboardingChannel:
    """Канал онбординга Messenger.wa: create+start сессии → поллинг → active."""

    messenger = Messenger.wa

    def __init__(self, session_factory=None, *, client_factory=None):
        self._session_factory = session_factory or async_session
        #: тесты подменяют REST-клиент целиком (сети в юнит-тестах нет).
        self._client_factory = client_factory
        self._logins: dict[int, _WaLoginState] = {}

    def _make_client(self) -> OpenWaClient:
        if self._client_factory is not None:
            return self._client_factory()
        settings = get_settings()
        return OpenWaClient(
            base_url=settings.wa_base_url,
            api_key=settings.wa_api_key,
            timeout=settings.wa_request_timeout_sec,
        )

    async def start(self, account: TgAccount, *, force: bool = False) -> dict:
        if account.status == TgAccountStatus.active and not force:
            return {"status": "already_active"}
        prev = self._logins.pop(account.id, None)
        if prev is not None:
            if prev.task is not None and not prev.task.done():
                prev.task.cancel()
            # Дедлайн живёт в отменённой задаче — сессию гасим здесь, иначе
            # каждый повторный QR оставляет движок (~30-80МБ) в сайдкаре.
            await self._cleanup_session(prev.session_id)

        settings = get_settings()
        client = self._make_client()
        try:
            info = await client.create_session(
                f"chatmost-{account.id}", proxy_url=settings.wa_proxy_url or None
            )
            await client.start_session(info["id"])
        except WaError as exc:
            await client.aclose()
            logger.error("WA create session failed: account_id=%s: %s", account.id, exc)
            return {"status": OnboardingStatus.error.value, "error": str(exc)}

        state = _WaLoginState(session_id=info["id"])
        state.task = asyncio.create_task(self._run(account, client, state, settings))
        self._logins[account.id] = state
        logger.info("WA QR-логин запущен: account_id=%s session=%s…", account.id, info["id"][:8])
        return {
            "status": OnboardingStatus.waiting.value,
            "qr_link": None,
            "detail": "qr_pending",
        }

    async def login_view(self, account_id: int) -> LoginView | None:
        state = self._logins.get(account_id)
        if state is None:
            return None
        return LoginView(status=state.status, qr_link=state.qr_link, error=state.error)

    async def submit_password(self, account_id: int, password: str) -> bool:
        return False  # QR linked-device флоу WA без 2FA-пароля

    async def cancel(self, account_id: int) -> None:
        state = self._logins.pop(account_id, None)
        if state is None:
            return
        if state.task is not None and not state.task.done():
            state.task.cancel()
        await self._cleanup_session(state.session_id)

    async def _cleanup_session(self, session_id: str) -> None:
        """Logout+delete в OpenWA best-effort (единый helper api-модуля)."""
        await wa_logout_and_delete(self._make_client(), session_id)

    async def _run(
        self, account: TgAccount, client: OpenWaClient, state: _WaLoginState, settings
    ) -> None:
        deadline = time.monotonic() + settings.wa_onboarding_deadline_sec
        try:
            while time.monotonic() < deadline:
                info = await client.get_session(state.session_id)
                status = info.get("status")
                if status == "ready":
                    await self._save_session(account, state, info)
                    return
                if status == "failed":
                    state.status = OnboardingStatus.error
                    state.error = str(info.get("lastError") or "session failed")
                    return
                # QR обновляем каждый проход (WA регенерит — страница перерисует).
                try:
                    qr = await client.session_qr(state.session_id)
                    if qr.get("qrCode"):
                        state.qr_link = qr["qrCode"]
                except WaError:
                    pass  # qr ещё не готов (400) — штатно на initializing
                await asyncio.sleep(_QR_POLL_SEC)
            state.status = OnboardingStatus.expired
            await self._cleanup_session(state.session_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            state.status = OnboardingStatus.error
            state.error = str(exc)
            logger.exception("WA onboarding failed: account_id=%s", account.id)
        finally:
            await client.aclose()

    async def _save_session(self, account: TgAccount, state: _WaLoginState, info: dict) -> None:
        """Креды линии → БД; status=active (bridge подхватит за ~20с)."""
        async with self._session_factory() as s:
            existing = await s.get(TgAccount, account.id)
            if existing is None:
                raise RuntimeError(f"WA: линия account_id={account.id} исчезла")
            existing.wa_session_id = state.session_id
            if info.get("phone"):
                existing.phone = str(info["phone"])
            if info.get("pushName"):
                existing.display_name = info["pushName"]
            existing.status = TgAccountStatus.active
            await s.commit()
        state.status = OnboardingStatus.authorized
        logger.info(
            "WA аккаунт сохранён: account_id=%s phone=%s", account.id, info.get("phone")
        )
