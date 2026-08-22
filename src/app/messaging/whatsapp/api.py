"""REST-клиент OpenWA-сайдкара.

Единственный командный транспорт WA-канала: сессии (создание/статус/QR/
logout), отправка текста и медиа, проверка контакта, скачивание входящего
медиа. Ошибки нормализуются в WaError(retryable) / WaAuthError — провайдер
и outbox HTTP-статусов не знают. 409/503 документированы как retryable,
429 — retry-after, 401 — терминальная проблема конфигурации ключа.
"""

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class WaError(Exception):
    """Сбой запроса к OpenWA. retryable=True → outbox-ретрай уместен."""

    def __init__(
        self, message: str, *, retryable: bool = False, retry_after_sec: int | None = None
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.retry_after_sec = retry_after_sec


class WaAuthError(WaError):
    """API-ключ OpenWA отвергнут (401) — терминально, чинить конфигурацию."""

    def __init__(self, message: str = "openwa api key rejected") -> None:
        super().__init__(message, retryable=False)


#: Статусы, которые спека OpenWA помечает retryable (409 conflict /
#: 503 dependency unavailable).
_RETRYABLE_STATUSES = {409, 503}


class OpenWaClient:
    """Тонкая httpx-обёртка; ``http_factory`` — seam для тестов (без сети)."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout: float = 20.0,
        http_factory=None,
    ) -> None:
        self._api_key = api_key
        self._http_factory = http_factory or (
            lambda: httpx.AsyncClient(base_url=base_url, timeout=timeout)
        )
        self._http: httpx.AsyncClient | None = None

    async def aclose(self) -> None:
        if self._http is not None and not self._http.is_closed:
            await self._http.aclose()

    # --- Сессии (жизненный цикл WA-линии) ---

    async def create_session(self, name: str, *, proxy_url: str | None = None) -> dict:
        body: dict[str, Any] = {"name": name}
        if proxy_url:
            body["proxyUrl"] = proxy_url
            body["proxyType"] = "socks5"
        return await self._json("POST", "/api/sessions", json=body)

    async def start_session(self, session_id: str) -> None:
        await self._json("POST", f"/api/sessions/{session_id}/start")

    async def get_session(self, session_id: str) -> dict:
        return await self._json("GET", f"/api/sessions/{session_id}")

    async def session_qr(self, session_id: str) -> dict:
        """{qrCode: PNG data-URL, status} — 400, пока QR не готов."""
        return await self._json("GET", f"/api/sessions/{session_id}/qr")

    async def logout_session(self, session_id: str) -> None:
        await self._json("POST", f"/api/sessions/{session_id}/logout")

    async def delete_session(self, session_id: str) -> None:
        await self._json("DELETE", f"/api/sessions/{session_id}")

    # --- Сообщения ---

    async def send_text(self, session_id: str, chat_id: str, text: str) -> dict:
        """201 {messageId, timestamp} — принято, НЕ доставлено (спека 6.3)."""
        return await self._json(
            "POST",
            f"/api/sessions/{session_id}/messages/send-text",
            json={"chatId": chat_id, "text": text},
        )

    async def send_media(
        self,
        session_id: str,
        chat_id: str,
        *,
        kind: str,
        b64: str,
        mimetype: str,
        filename: str | None = None,
        caption: str | None = None,
    ) -> dict:
        """kind: image|video|audio|document|sticker (flat-DTO спеки 6.3)."""
        body: dict[str, Any] = {"chatId": chat_id, "base64": b64, "mimetype": mimetype}
        if filename:
            body["filename"] = filename
        if caption:
            body["caption"] = caption
        return await self._json(
            "POST", f"/api/sessions/{session_id}/messages/send-{kind}", json=body
        )

    async def check_contact(self, session_id: str, number: str) -> dict:
        """{number, exists, whatsappId} — резолв «написать первым»."""
        return await self._json("GET", f"/api/sessions/{session_id}/contacts/check/{number}")

    async def download_media(
        self, session_id: str, chat_id: str, message_id: str
    ) -> tuple[bytes, str | None]:
        """Бинарный ответ + mimetype из content-type."""
        resp = await self._send(
            "GET", f"/api/sessions/{session_id}/messages/{chat_id}/{message_id}/media"
        )
        return resp.content, resp.headers.get("content-type")

    # --- Внутреннее ---

    async def _json(self, method: str, path: str, *, json: dict | None = None) -> Any:
        resp = await self._send(method, path, json=json)
        if not resp.content:
            return None
        if "json" not in resp.headers.get("content-type", ""):
            raise WaError(f"openwa: не-JSON ответ на {path}")
        return resp.json()

    async def _send(self, method: str, path: str, *, json: dict | None = None) -> httpx.Response:
        if self._http is None or self._http.is_closed:
            self._http = self._http_factory()
        try:
            resp = await self._http.request(
                method, path, json=json, headers={"X-API-Key": self._api_key}
            )
        except httpx.HTTPError as exc:
            raise WaError(f"openwa transport: {exc}", retryable=True) from exc
        if resp.status_code == 401:
            raise WaAuthError()
        if resp.status_code >= 400:
            raise WaError(
                f"openwa {resp.status_code} {path}: {_error_detail(resp)}",
                retryable=resp.status_code in _RETRYABLE_STATUSES,
                retry_after_sec=_retry_after(resp),
            )
        return resp


async def wa_logout_and_delete(client: OpenWaClient, session_id: str) -> None:
    """Logout+delete сессии OpenWA best-effort: покинутая qr_ready-сессия
    держит движок (~30-80МБ RAM). Единственная реализация — фабрика и
    онбординг делят её (дрейф дублей здесь = утечка памяти сайдкара)."""
    try:
        try:
            await client.logout_session(session_id)
        except WaError:
            pass  # не-готовая сессия умирает и так — delete добьёт
        await client.delete_session(session_id)
    except WaError as exc:
        logger.warning("WA cleanup session %s… failed: %s", session_id[:8], exc)
    finally:
        await client.aclose()


def _error_detail(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:200]
    if isinstance(body, dict):
        return str(body.get("message") or body)[:200]
    return str(body)[:200]


def _retry_after(resp: httpx.Response) -> int | None:
    if resp.status_code != 429:
        return None
    raw = resp.headers.get("retry-after", "")
    return int(raw) if raw.isdigit() else 30
