import enum
from dataclasses import dataclass
from datetime import datetime

from app.models import Messenger


class ContentType(str, enum.Enum):
    text = "text"
    photo = "photo"
    file = "file"
    video = "video"
    voice = "voice"
    sticker = "sticker"


@dataclass
class IncomingMessage:
    """Сообщение, пришедшее из мессенджера (канал-нейтрально).

    ``external_*``-поля — строковые внешние id: у TG это числовые id MTProto,
    у MAX — числовые id web-протокола; строка безопасна для обоих (id MAX
    длинные, а Bot API отдаёт строковые mid).
    """

    messenger: Messenger  # канал, из которого пришло сообщение
    external_chat_id: str  # id чата в канале (у TG == id клиента в приватных)
    sender_external_id: str  # кто написал (клиент)
    sender_name: str | None
    sender_phone: str | None
    sender_username: str | None
    content_type: ContentType
    text: str | None = None
    media_path: str | None = None
    external_message_id: str | None = None
    timestamp: datetime | None = None
    is_reply: bool = False  # True если это ответ клиента (диалог уже существует)
    # Раздельные имя/фамилия, если канал дал (TG: first/last; MAX: names[]);
    # sender_name остаётся полным отображаемым именем (виджет, уведомления).
    sender_first_name: str | None = None
    sender_last_name: str | None = None


@dataclass
class SendResult:
    """Результат отправки: провайдер сам знает, из какого он аккаунта."""

    success: bool
    external_message_id: str | None = None
    error: str | None = None
    # Сколько секунд подождать до повтора (FloodWait TG / throttle MAX).
    retry_after_seconds: int | None = None
