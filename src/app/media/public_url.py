"""Подписанные публичные URL (медиа-вложения, гашение уведомлений).

imconnector.send.messages требует ПУБЛИЧНУЮ ссылку на файл — B24 качает
его своими серверами; кнопка «Отвечать не нужно» — та же механика. Ключ —
session_secret (класс последствий тот же, что у сессионных кук; отдельный
секрет = новая ручка без новой угрозы). Ссылки не персонифицированы и не
отзывны: компромисс осознанный, TTL ограничивает окно.

Scope-префикс разводит подписи разных назначений при одном секрете;
пустой scope у медиа сохраняет исторический формат f"{id}:{exp}"
байт-в-байт — ссылки, выданные до появления scope, остаются валидны.
"""

import hashlib
import hmac
import time


def sign_scoped_url(
    base_url: str, path_prefix: str, ident: int, *, secret: str, ttl_sec: int, scope: str = ""
) -> str:
    """Публичный URL {path_prefix}/{ident}/{exp}/{sig} с HMAC и сроком."""
    exp = int(time.time()) + ttl_sec
    return f"{base_url.rstrip('/')}{path_prefix}/{ident}/{exp}/{_sig(secret, ident, exp, scope)}"


def verify_scoped_sig(
    ident: int, exp: int, sig: str, *, secret: str, scope: str = ""
) -> bool:
    """Проверить подпись и срок (просроченная/чужая — False)."""
    if not isinstance(sig, str) or exp < time.time():
        return False
    # Байты: compare_digest(str, str) падает TypeError на не-ASCII из URL.
    return hmac.compare_digest(
        _sig(secret, ident, exp, scope).encode("ascii"), sig.encode("utf-8")
    )


def _sig(secret: str, ident: int, exp: int, scope: str) -> str:
    payload = f"{scope}:{ident}:{exp}" if scope else f"{ident}:{exp}"
    # 128 бит достаточно для неугадываемости; hex — urlsafe.
    return hmac.new(
        secret.encode(), payload.encode(), hashlib.sha256
    ).hexdigest()[:32]


# --- Медиа (исторический контракт imconnector.files[].url) ---


def sign_media_url(
    base_url: str, attachment_id: int, *, secret: str, ttl_sec: int
) -> str:
    """Публичный URL вложения с HMAC-подписью и сроком действия."""
    return sign_scoped_url(
        base_url, "/media/public", attachment_id, secret=secret, ttl_sec=ttl_sec
    )


def verify_media_sig(
    attachment_id: int, exp: int, sig: str, *, secret: str
) -> bool:
    return verify_scoped_sig(attachment_id, exp, sig, secret=secret)
