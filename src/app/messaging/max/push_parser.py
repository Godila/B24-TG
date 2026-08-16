"""Толерантный разбор push'ей MAX → IncomingMessage-поля.

Единственная точка знания о структуре входящих (правится по живым логам
без риска для остального кода). Форматы пойманы живьём 2026-08-15/16,
push op=128 (обновление чата) приходит в ДВУХ формах:

  первое сообщение чата (полный объект):
    payload = {chatId, unread, chat: {type: "DIALOG", ...,
      lastMessage: {sender: int, id: "...", time: ms, text, type: "USER",
                    attaches: [], elements: []}}}

  последующие сообщения (лёгкий пуш — пойман 2026-08-16 на e2e):
    payload = {chatId, unread, message: {sender, id, time, text, type, ...}}

Во второй форме НЕТ chat.type — фильтр групповых чатов для неё делает
провайдер через CHAT_INFO по незнакомому chatId (см. provider.py).

Фильтры v1: только личные диалоги, только чужие сообщения (sender !=
own_user_id — свой MSG_SEND тоже прилетает chat-update), Избранное
(chatId == 0) и служебные типы сообщений скипаются.
"""

from dataclasses import dataclass
from typing import Any

from app.messaging.max.protocol import (
    OP_CHAT_ACTIVITY,
    OP_CHAT_UPDATE,
    ms_to_datetime,
)
from app.messaging.types import ContentType

#: Значения chat.type, которые считаем личным диалогом клиент↔менеджер.
_DIALOG_TYPES = {"DIALOG"}


def _as_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def contact_display_name(contact: dict) -> str | None:
    """Отображаемое имя из объекта contact (ответ GET_CONTACTS).

    names: [{name, firstName, lastName, type: FULL_NAME|ONEME|...}] —
    предпочитаем FULL_NAME, иначе первое непустое; конкатенация
    firstName+lastName как запасной путь.
    """
    names = contact.get("names")
    if isinstance(names, list) and names:
        entries = [n for n in names if isinstance(n, dict)]
        for n in entries:
            if str(n.get("type") or "").upper() == "FULL_NAME" and n.get("name"):
                return str(n["name"])
        for n in entries:
            if n.get("name"):
                return str(n["name"])
        for n in entries:
            combined = " ".join(p for p in (n.get("firstName"), n.get("lastName")) if p).strip()
            if combined:
                return combined
    combined = " ".join(p for p in (contact.get("firstName"), contact.get("lastName")) if p).strip()
    return combined or None


def contact_phone(contact: dict) -> str | None:
    """Первый телефон из contact.phones[{number, type}] (может не быть)."""
    phones = contact.get("phones")
    if isinstance(phones, list):
        for p in phones:
            if isinstance(p, dict) and p.get("number"):
                return str(p["number"])
    return None


def contact_name_parts(contact: dict) -> tuple[str | None, str | None]:
    """(firstName, lastName) из объекта contact (ответ GET_CONTACTS).

    Для CRM-карточки (NAME/LAST_NAME по отдельности). Берём из записи
    names[] с типом FULL_NAME (паспортное имя), иначе из первой записи,
    где они есть; без split — (None, None) и карточка получает отображаемое
    имя целиком в NAME.
    """
    names = contact.get("names")
    entries = [n for n in names if isinstance(n, dict)] if isinstance(names, list) else []
    ordered = sorted(
        entries,
        key=lambda n: 0 if str(n.get("type") or "").upper() == "FULL_NAME" else 1,
    )
    for n in ordered:
        first, last = n.get("firstName"), n.get("lastName")
        if first or last:
            return (str(first) if first else None, str(last) if last else None)
    return (None, None)


@dataclass(slots=True)
class ParsedPush:
    """Результат разбора: либо валидное входящее, либо причина скипа."""

    external_chat_id: str | None = None
    sender_external_id: str | None = None
    external_message_id: str | None = None
    text: str | None = None
    timestamp: Any = None
    content_type: ContentType | None = None
    is_reply: bool = False
    #: True когда chat.type был в пуше (полная форма). В лёгкой форме
    #: (payload.message) тип чата неизвестен — провайдер проверит его
    #: через CHAT_INFO по незнакомому chatId.
    chat_type_known: bool = False
    #: None = сообщение; иначе причина скипа ('favorites'|'group'|'self'|
    #: 'service'|'empty') — провайдер логирует и не кладёт в очередь.
    skip_reason: str | None = "empty"


def _content_type_and_text(msg: dict) -> tuple[ContentType, str | None]:
    """Тип контента: как у TG — медиа без текста не теряется, а плейсхолдер."""
    text = msg.get("text") or None
    attaches = msg.get("attaches")
    if not isinstance(attaches, list) or not attaches:
        return ContentType.text, text
    first = attaches[0] if isinstance(attaches[0], dict) else {}
    kind = str(first.get("type") or "").upper()
    # Известные типы вложений web-клиента: IMAGE/VIDEO/AUDIO/FILE.
    if kind.startswith("IMAGE"):
        return ContentType.photo, text or "[фото]"
    if kind.startswith("VIDEO"):
        return ContentType.video, text or "[видео]"
    if kind.startswith("AUDIO"):
        return ContentType.voice, text or "[голосовое сообщение]"
    # INLINE_KEYBOARD (боты) и прочее — файл-плейсхолдер, текст важнее.
    return ContentType.file, text or "[вложение]"


def parse_message_push(frame: dict, own_user_id: int | None) -> ParsedPush:
    """Разобрать push-фрейм MAX. Не бросает исключений — только best-effort."""
    result = ParsedPush()
    op = frame.get("opcode")
    if op == OP_CHAT_ACTIVITY:
        result.skip_reason = "activity"  # typing/просмотр — не сообщение
        return result
    if op != OP_CHAT_UPDATE:
        result.skip_reason = f"op_{op}"
        return result

    payload = frame.get("payload") or {}
    chat = payload.get("chat") if isinstance(payload.get("chat"), dict) else {}
    # Полный пуш несёт chat.lastMessage; лёгкий (2-е+ сообщения) — payload.message.
    msg = chat.get("lastMessage") or payload.get("lastMessage") or payload.get("message")
    if not isinstance(msg, dict) or not msg:
        result.skip_reason = "no_message"
        return result

    chat_id_raw = payload.get("chatId", chat.get("id"))
    chat_id = _as_str(chat_id_raw)
    if chat_id is None:
        result.skip_reason = "no_chat_id"
        return result
    if chat_id == "0":
        result.skip_reason = "favorites"
        return result

    chat_type = str(chat.get("type") or "").upper()
    if chat_type and chat_type not in _DIALOG_TYPES:
        result.skip_reason = f"group_{chat_type.lower()}"
        return result
    sender = _as_str(msg.get("sender"))
    if sender is None:
        result.skip_reason = "no_sender"
        return result
    if own_user_id is not None and sender == str(own_user_id):
        result.skip_reason = "self"
        return result

    msg_type = str(msg.get("type") or "USER").upper()
    if msg_type not in ("USER", ""):
        # SERVICE/SYSTEM и пр. служебные записи чата.
        result.skip_reason = f"service_{msg_type.lower()}"
        return result

    external_id = _as_str(msg.get("id"))
    ctype, text = _content_type_and_text(msg)
    if external_id is None and text is None:
        result.skip_reason = "empty"
        return result

    result.external_chat_id = chat_id
    result.sender_external_id = sender
    result.external_message_id = external_id
    result.text = text
    result.timestamp = ms_to_datetime(msg.get("time"))
    result.content_type = ctype
    result.is_reply = bool(msg.get("replyTo") or msg.get("replyToId"))
    result.chat_type_known = bool(chat_type)
    result.skip_reason = None
    return result
