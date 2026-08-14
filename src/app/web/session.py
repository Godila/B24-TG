"""Сессионная кука (HMAC-подписанный payload) для Web UI.

Placement-флоу: B24 передаёт реальный user_id менеджера -> мы выставляем
куку с payload {b24_user_id, deal_id, exp}, подписанную HMAC-SHA256.
Дальнейшие API-запросы валидируются по этой куке."""

import base64
import hashlib
import hmac
import json
import time
from typing import Any

SESSION_COOKIE = "btg_sess"
SESSION_TTL = 8 * 3600  # 8 часов (рабочий день)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def create_session_payload(
    b24_user_id: int, deal_id: int | None = None, ttl: int = SESSION_TTL
) -> dict[str, Any]:
    """Построить payload сессии. ``ttl`` может быть отрицательным для тестов истечения."""
    exp = int(time.time()) + ttl
    return {"b24_user_id": b24_user_id, "deal_id": deal_id, "exp": exp}


def sign_session(payload: dict[str, Any], secret: str) -> str:
    """Подписать payload HMAC-SHA256 -> токен 'payload.signature'."""
    payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    sig = hmac.new(secret.encode(), payload_bytes, hashlib.sha256).digest()
    return _b64(payload_bytes) + "." + _b64(sig)


def verify_session(token: str, secret: str) -> dict[str, Any] | None:
    """Проверить подпись и срок. Вернуть payload или None если невалиден.

    Подпись сравнивается как каноническая base64url-строка (constant-time через
    ``hmac.compare_digest``): это отвергает любые изменения подписи, включая
    модификации битов паддинга в последнем символе, которые декодировали бы в те
    же байты, но не являются каноническим представлением ожидаемого HMAC.
    """
    try:
        payload_b64, sig_b64 = token.split(".", 1)
    except ValueError:
        return None
    expected_sig_b64 = _b64(
        hmac.new(secret.encode(), _unb64(payload_b64), hashlib.sha256).digest()
    )
    if not hmac.compare_digest(sig_b64, expected_sig_b64):
        return None
    try:
        payload = json.loads(_unb64(payload_b64))
    except ValueError:
        # binascii.Error и json.JSONDecodeError — оба подклассы ValueError.
        return None
    if not isinstance(payload, dict):
        return None
    exp = payload.get("exp")
    if not isinstance(exp, int) or time.time() >= exp:
        return None
    return payload


def create_session_cookie_params(
    b24_user_id: int, deal_id: int | None, secret: str, *, secure: bool = True
) -> dict[str, Any]:
    """Параметры для response.set_cookie(...) — готовый токен и настройки.

    ``secure=True`` по умолчанию (prod за HTTPS); для dev-сервера на
    http://localhost передавай ``secure=False``.

    SameSite: виджет живёт в кросс-сайтовом iframe на портале B24
    (b24-*.bitrix24.ru → b24-tg.haragy.top) — Lax-куки браузеры в такой
    контекст не отправляют (401 на каждом /api-вызове из iframe). Поэтому
    в prod ставим ``none`` (требует Secure), в dev — ``lax``.
    """
    payload = create_session_payload(b24_user_id, deal_id)
    token = sign_session(payload, secret)
    return {
        "key": SESSION_COOKIE,
        "value": token,
        "httponly": True,
        "samesite": "none" if secure else "lax",
        "max_age": SESSION_TTL,
        "path": "/",
        "secure": secure,
    }
