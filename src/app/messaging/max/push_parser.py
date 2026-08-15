"""Толерантный разбор push'ей MAX → IncomingMessage-поля.

Единственная точка знания о структуре входящих (правится по живым логам
без риска для остального кода). Формат пойман живьём 2026-08-15:

    push op=128 (обновление чата):
      payload = {chatId: int, unread: int, chat: {type: "DIALOG", ...,
        lastMessage: {sender: int, id: "117099261900910729", time: ms,
                      text: str, type: "USER", attaches: [], elements: []}}}

Фильтры v1: только личные диалогы (chat.type == "DIALOG"), только чужие
сообщения (sender != own_user_id — свой MSG_SEND тоже прилетает chat-update),
Избранное (chatId == 0) и служебные типы сообщений скипаются.
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
    msg = chat.get("lastMessage") or payload.get("lastMessage")
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
    result.skip_reason = None
    return result
