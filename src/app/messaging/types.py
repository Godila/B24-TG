import enum
from dataclasses import dataclass
from datetime import datetime


class ContentType(str, enum.Enum):
    text = "text"
    photo = "photo"
    file = "file"
    video = "video"
    voice = "voice"
    sticker = "sticker"


@dataclass
class IncomingMessage:
    """Сообщение, пришедшее из мессенджера."""

    account_id: int  # id tg_accounts (на какой аккаунт пришло)
    external_chat_id: str  # TG chat id как строка
    sender_tg_id: int  # кто написал (клиент)
    sender_name: str | None
    sender_phone: str | None
    sender_username: str | None
    content_type: ContentType
    text: str | None = None
    media_path: str | None = None
    external_message_id: int | None = None
    timestamp: datetime | None = None
    is_reply: bool = False  # True если это ответ клиента (диалог уже существует)


@dataclass
class SendResult:
    success: bool
    external_message_id: int | None = None
    error: str | None = None
    flood_wait_seconds: int | None = None  # если TG прислал FloodWait


class DeliveryStatus(str, enum.Enum):
    sent = "sent"
    delivered = "delivered"
    read = "read"
    failed = "failed"
