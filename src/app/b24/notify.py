"""Сборка feed-уведомления менеджеру (Wazzup-паритет): текст, KEYBOARD,
URL карточки CRM и подписанная ссылка «Отвечать не нужно».

Формат KEYBOARD ({"BUTTONS": [[кнопка, …]]}, TYPE "link") сверен живым
спайком scripts/spike_im_notification.py.
"""

from app.media.public_url import sign_scoped_url, verify_scoped_sig

#: Обрезка текста сообщения в уведомлении (фид читается бегло).
MAX_TEXT_LEN = 400

_DISMISS_SCOPE = "notify-dismiss"


def build_notification_text(
    label: str, name: str, text: str | None, unanswered_count: int
) -> str:
    """Текст строки фида: канал, клиент, последнее входящее, счётчик."""
    body = (text or "").strip() or "[вложение]"
    if len(body) > MAX_TEXT_LEN:
        body = body[:MAX_TEXT_LEN].rstrip() + "…"
    lines = [f"💬 Новое сообщение в {label} от {name}:", body]
    if unanswered_count > 1:
        lines.append(f"Неотвеченных сообщений: {unanswered_count}")
    return "\n".join(lines)


def crm_card_url(
    portal: str,
    *,
    entity_id: int | None,
    entity_type: str | None,
    contact_id: int | None,
) -> str | None:
    """Ссылка «Открыть диалог»: карточка сделки/лида (там живёт виджет
    ЧатМоста), фолбэк — карточка контакта. Ничего нет — None (без кнопки)."""
    base = portal.rstrip("/")
    if entity_id is not None:
        kind = "lead" if entity_type == "lead" else "deal"
        return f"{base}/crm/{kind}/{entity_id}/view/"
    if contact_id is not None:
        return f"{base}/crm/contact/{contact_id}/view/"
    return None


def build_keyboard(card_url: str | None, dismiss_url: str | None) -> dict | None:
    """KEYBOARD для im.message.add: один ряд LINK-кнопок (LINK не требует
    регистрации чат-бота). Нет ни одной — None (клавиатуры не будет)."""
    row: list[dict] = []
    if card_url:
        row.append({"TYPE": "link", "TEXT": "Открыть диалог", "LINK": card_url})
    if dismiss_url:
        row.append({"TYPE": "link", "TEXT": "Отвечать не нужно", "LINK": dismiss_url})
    return {"BUTTONS": [row]} if row else None


def sign_dismiss_url(
    base_url: str, dialog_id: int, *, secret: str, ttl_sec: int
) -> str:
    """Публичный URL «Отвечать не нужно» с HMAC-подписью и сроком действия.

    Не персонифицирована и не отзывна — как медиа-ссылки: гашение безвредно
    при утечке (следующее входящее снова уведомит), TTL ограничивает окно.
    """
    return sign_scoped_url(
        base_url, "/notify/dismiss", dialog_id, secret=secret, ttl_sec=ttl_sec, scope=_DISMISS_SCOPE
    )


def verify_dismiss_sig(dialog_id: int, exp: int, sig: str, *, secret: str) -> bool:
    """Проверить подпись и срок (просроченная/чужая — False)."""
    return verify_scoped_sig(dialog_id, exp, sig, secret=secret, scope=_DISMISS_SCOPE)
