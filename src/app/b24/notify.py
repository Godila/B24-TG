"""Сборка feed-уведомления менеджеру (Wazzup-паритет): текст, KEYBOARD,
URL карточки CRM и подписанная ссылка «Отвечать не нужно».

Формат KEYBOARD ({"BUTTONS": [кнопка, …]}, плоский; TYPE только NEWLINE) сверен живым
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
    ЧатМоста), фолбэк — карточка контакта. Ничего нет — None (без кнопки).

    Формат /crm/{сущность}/details/{id}/ — живой прогон 08-22: прежний
    /view/ редиректил на СПИСОК сущностей вместо карточки."""
    base = portal.rstrip("/")
    if entity_id is not None:
        kind = "lead" if entity_type == "lead" else "deal"
        return f"{base}/crm/{kind}/details/{entity_id}/"
    if contact_id is not None:
        return f"{base}/crm/contact/details/{contact_id}/"
    return None


def build_keyboard(card_url: str | None, dismiss_button: dict | None) -> dict | None:
    """KEYBOARD: {"BUTTONS": [кнопка, …]} — плоский массив (формат сверен
    живым спайком scripts/spike_im_notification.py). Каждая кнопка отдельной
    строкой (DISPLAY=BLOCK, дефолт). Нет ни одной — None."""
    buttons: list[dict] = []
    if card_url:
        buttons.append({"TEXT": "Открыть диалог", "LINK": card_url})
    if dismiss_button:
        buttons.append(dismiss_button)
    return {"BUTTONS": buttons} if buttons else None


def dismiss_command_button(dialog_id: int) -> dict:
    """COMMAND-кнопка гашения (чат-бот): клик инлайн, без вкладок; BLOCK —
    гасить кнопку после нажатия (повторные клики не плодят события)."""
    return {
        "TEXT": "Отвечать не нужно",
        "COMMAND": "dismiss",
        "COMMAND_PARAMS": str(dialog_id),
        "BLOCK": "Y",
    }


def dismiss_link_button(url: str) -> dict:
    """LINK-фолбэк без бота: подписанная страница /notify/dismiss."""
    return {"TEXT": "Отвечать не нужно", "LINK": url}


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
