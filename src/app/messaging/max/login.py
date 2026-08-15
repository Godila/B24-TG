"""MaxQrLoginFlow — QR-вход в MAX для онбординга менеджера (web-процесс).

Флоу (проверен спайком S0): INIT → QR_AUTH_REQUEST(288) → QR в браузере →
поллинг 289 (trackId) → status.loginAvailable → QR_AUTH_LOGIN(291) →
tokenAttrs.LOGIN.token + profile; passwordChallenge → 115 {trackId, password}.

Соединение флоу короткоживущее: после 291 оно уже ONLINE (токен получен),
LOGIN(19) здесь НЕ делается — первое восстановление сессии выполнит bridge.
Это бережёт «LOGIN-бюджет» токена (~30-50 быстрых LOGIN сбрасывают его).
"""

import asyncio
import enum
import logging
import time
import uuid
from dataclasses import dataclass

from app.messaging.max.protocol import (
    OP_INIT,
    OP_PING,
    OP_QR_AUTH_LOGIN,
    OP_QR_AUTH_POLL,
    OP_QR_AUTH_REQUEST,
    OP_QR_PASSWORD,
    MaxQrDisabledError,
    MaxQrExpiredError,
    extract_token,
    init_payload,
)
from app.messaging.max.ws_client import MaxWsClient

logger = logging.getLogger(__name__)


class QrFlowStatus(str, enum.Enum):
    waiting = "waiting"
    password_required = "password_required"
    authorized = "authorized"
    expired = "expired"
    error = "error"


@dataclass(slots=True)
class MaxSession:
    """Результат успешного QR-входа — сохраняется в tg_accounts."""

    token: str
    device_id: str
    max_user_id: int
    name: str | None
    phone: str | None


class MaxQrLoginFlow:
    """Машина состояний одного QR-логина; живёт в web-процессе.

    ``run()`` — фоновая корутина (роут стартует её как таск); страница
    опрашивает ``status``/``qr_link``; 2FA-пароль подаётся через
    ``submit_password()`` (ждёт ``password_required``).
    """

    def __init__(
        self,
        *,
        client: MaxWsClient,
        device_id: str | None = None,
        user_agent: dict,
        deadline_sec: float = 300.0,
        password_timeout_sec: float = 120.0,
        poll_interval_sec: float = 5.0,
    ):
        self._client = client
        self._device_id = device_id or str(uuid.uuid4())
        self._user_agent = user_agent
        self._deadline_sec = deadline_sec
        self._password_timeout_sec = password_timeout_sec
        self._default_poll_sec = poll_interval_sec

        self.status = QrFlowStatus.waiting
        self.error: str | None = None
        self.qr_link: str | None = None
        self.result: MaxSession | None = None

        self._password_event = asyncio.Event()
        self._password_value: str | None = None

    def submit_password(self, password: str) -> bool:
        """Подать 2FA-пароль; False — флоу не ждёт пароль."""
        if self.status is not QrFlowStatus.password_required:
            return False
        self._password_value = password
        self._password_event.set()
        return True

    async def run(self) -> None:
        try:
            await self._run_inner()
        except asyncio.CancelledError:
            raise
        except MaxQrDisabledError:
            self.status = QrFlowStatus.error
            self.error = (
                "MAX отклонил QR-вход: версия web-клиента устарела "
                "(qr_login.disabled). Обновите MAX_APP_VERSION в настройках."
            )
            logger.error("QR-флоу: qr_login.disabled — appVersion устарела")
        except Exception as exc:
            self.status = QrFlowStatus.error
            self.error = f"{type(exc).__name__}: {exc}"
            logger.exception("MAX QR-флоу упал")
        finally:
            # 2FA-пароль не должен переживать флоу ни при каком исходе.
            self._password_value = None
            await self._safe_close()

    async def _run_inner(self) -> None:
        await self._client.connect()
        await self._client.request(OP_INIT, init_payload(self._device_id, self._user_agent))
        await self._request_qr()

        deadline = time.monotonic() + self._deadline_sec
        while time.monotonic() < deadline:
            await asyncio.sleep(self._default_poll_sec)
            try:
                resp = await self._client.request(
                    OP_QR_AUTH_POLL, {"trackId": self._track_id}, timeout=10.0
                )
            except MaxQrExpiredError:
                # QR истёк/использован — новый 288, страница перерисует.
                await self._request_qr()
                continue
            status = ((resp.get("payload") or {}).get("status")) or {}
            if not status.get("loginAvailable"):
                continue

            auth_payload = await self._finish_login()
            self._store_result(auth_payload)
            return

        self.status = QrFlowStatus.expired
        logger.warning("MAX QR-флоу: время ожидания скана истекло")

    async def _request_qr(self) -> None:
        resp = await self._client.request(OP_QR_AUTH_REQUEST)
        payload = resp.get("payload") or {}
        link = payload.get("qrLink")
        track_id = payload.get("trackId")
        if not link or not track_id:
            raise RuntimeError(f"неожиданный ответ QR_AUTH_REQUEST: {payload}")
        self.qr_link = link
        self._track_id = track_id
        if isinstance(payload.get("pollingInterval"), (int, float)):
            self._default_poll_sec = payload["pollingInterval"] / 1000.0
        self.status = QrFlowStatus.waiting
        logger.info("MAX QR выдан (trackId=%s…)", str(track_id)[:8])

    async def _finish_login(self) -> dict:
        """291 (+115 при 2FA): возвращает payload с токеном и профилем."""
        resp = await self._client.request(
            OP_QR_AUTH_LOGIN, {"trackId": self._track_id}, timeout=20.0
        )
        payload = resp.get("payload") or {}
        if payload.get("passwordChallenge"):
            self.status = QrFlowStatus.password_required
            logger.info("MAX QR-флоу: нужен 2FA-пароль")
            try:
                # Пока ждём ввод — поддерживаем соединение своим ping'ом.
                await asyncio.wait_for(
                    self._wait_password_with_ping(), timeout=self._password_timeout_sec
                )
            except TimeoutError:
                raise RuntimeError("ввод 2FA-пароля затянулся (соединение закрыто)")
            if not self._password_value:
                raise RuntimeError("2FA-пароль не получен")
            resp = await self._client.request(
                OP_QR_PASSWORD,
                {"trackId": self._track_id, "password": self._password_value},
                timeout=20.0,
            )
            payload = resp.get("payload") or {}
        return payload

    async def _wait_password_with_ping(self) -> None:
        while not self._password_event.is_set():
            await self._client.request(OP_PING, {"interactive": True}, timeout=10.0)
            try:
                await asyncio.wait_for(self._password_event.wait(), timeout=15.0)
            except TimeoutError:
                continue

    def _store_result(self, auth_payload: dict) -> None:
        token = extract_token(auth_payload)
        if not token:
            raise RuntimeError("в ответе авторизации нет токена (сессия привязана к соединению)")
        profile = auth_payload.get("profile") or {}
        contact = profile.get("contact") or profile
        if contact.get("id") is None:
            # Без собственного user_id провайдер не сможет фильтровать
            # self-эхо — каждое исходящее сообщение менеджера стало бы
            # «входящим от клиента».
            raise RuntimeError("в профиле авторизации нет user_id")
        name = " ".join(
            filter(None, [contact.get("firstName"), contact.get("lastName")])
        ) or contact.get("nick")
        phones = profile.get("phones") or contact.get("phones") or []
        phone = None
        if phones and isinstance(phones[0], dict):
            phone = phones[0].get("number") or phones[0].get("phone")
        elif phones:
            phone = str(phones[0])
        self.result = MaxSession(
            token=token,
            device_id=self._device_id,
            max_user_id=contact.get("id"),
            name=name,
            phone=phone,
        )
        self.status = QrFlowStatus.authorized
        logger.info(
            "MAX QR-вход выполнен: user_id=%s name=%s",
            self.result.max_user_id, self.result.name,
        )

    async def _safe_close(self) -> None:
        try:
            await self._client.close()
        except Exception:
            logger.debug("onboarding close best-effort", exc_info=True)
