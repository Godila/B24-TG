"""Подписанные публичные URL медиа-вложений.

imconnector.send.messages требует ПУБЛИЧНУЮ ссылку на файл — B24 качает
его своими серверами. Ключ — session_secret (класс последствий тот же,
что у сессионных кук; отдельный секрет = новая ручка без новой угрозы).
Ссылка не персонифицирована и не отзывная: компромисс осознанный, TTL
ограничивает окно (см. Settings.media_public_ttl_sec).
"""

import hashlib
import hmac
import time


def sign_media_url(
    base_url: str, attachment_id: int, *, secret: str, ttl_sec: int
) -> str:
    """Публичный URL вложения с HMAC-подписью и сроком действия."""
    exp = int(time.time()) + ttl_sec
    return f"{base_url.rstrip('/')}/media/public/{attachment_id}/{exp}/{_sig(secret, attachment_id, exp)}"


def verify_media_sig(
    attachment_id: int, exp: int, sig: str, *, secret: str
) -> bool:
    """Проверить подпись и срок (просроченная/чужая — False)."""
    if not isinstance(sig, str) or exp < time.time():
        return False
    # Байты: compare_digest(str, str) падает TypeError на не-ASCII из URL.
    return hmac.compare_digest(
        _sig(secret, attachment_id, exp).encode("ascii"), sig.encode("utf-8")
    )


def _sig(secret: str, attachment_id: int, exp: int) -> str:
    # 128 бит достаточно для неугадываемости; hex — urlsafe.
    return hmac.new(
        secret.encode(), f"{attachment_id}:{exp}".encode(), hashlib.sha256
    ).hexdigest()[:32]
