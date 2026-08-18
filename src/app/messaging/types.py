import enum
from dataclasses import dataclass
from datetime import datetime

from app.models import MessageDirection, Messenger


class ContentType(str, enum.Enum):
    text = "text"
    photo = "photo"
    file = "file"
    video = "video"
    voice = "voice"
    sticker = "sticker"


#: Текст-плейсхолдеры медиа-сообщений: единственный источник — здесь.
#: Их видит B24-timeline (комментарий) и превью списка «Чатов»; UI пузырь
#: при наличии вложения плейсхолдер скрывает (см. _message_dto).
MEDIA_PLACEHOLDERS: dict["ContentType", str] = {
    ContentType.photo: "[фото]",
    ContentType.video: "[видео]",
    ContentType.voice: "[голосовое сообщение]",
    ContentType.sticker: "[стикер]",
    ContentType.file: "[файл]",
}


@dataclass
class MediaPayload:
    """Медиа-файл, уже сохранённый на диск провайдером (общий том media).

    Канало-нейтрально: TG создаёт в момент ``download_media``, MAX — при
    скачивании своего вложения (MaxMediaClient.download). ``path`` —
    относительный путь внутри медиа-тома (POSIX), резолвит MediaStorage.
    """

    path: str
    mime_type: str | None = None
    size: int | None = None
    file_name: str | None = None


@dataclass
class IncomingMessage:
    """Сообщение, наблюдаемое из канала (канало-нейтрально).

    ``external_*``-поля — строковые внешние id: у TG это числовые id MTProto,
    у MAX — числовые id web-протокола; строка безопасна для обоих (id MAX
    длинные, а Bot API отдаёт строковые mid).

    ``direction=outbound`` — сообщение написано менеджером с устройства
    (не из виджета): провайдер уже отделил его от эха собственных отправок.
    ``sender_*`` в этой ветке могут описывать самого владельца аккаунта —
    IncomingHandler их не использует (контакт берётся из существующего
    диалога).
    """

    messenger: Messenger  # канал, из которого пришло сообщение
    external_chat_id: str  # id чата в канале (у TG == id клиента в приватных)
    sender_external_id: str  # кто написал (клиент)
    sender_name: str | None
    sender_phone: str | None
    sender_username: str | None
    content_type: ContentType
    text: str | None = None
    #: Скачанное медиа (None → в text остаётся плейсхолдер «[фото]» и т.п.).
    media: MediaPayload | None = None
    external_message_id: str | None = None
    timestamp: datetime | None = None
    is_reply: bool = False  # True если это ответ клиента (диалог уже существует)
    # Раздельные имя/фамилия, если канал дал (TG: first/last; MAX: names[]);
    # sender_name остаётся полным отображаемым именем (виджет, уведомления).
    sender_first_name: str | None = None
    sender_last_name: str | None = None
    #: Направление: inbound (по умолчанию — все существующие call-сайты)
    #: или outbound — написано менеджером с устройства (device-outbound).
    direction: MessageDirection = MessageDirection.inbound


@dataclass
class SendResult:
    """Результат отправки: провайдер сам знает, из какого он аккаунта."""

    success: bool
    external_message_id: str | None = None
    error: str | None = None
    # Сколько секунд подождать до повтора (FloodWait TG / throttle MAX).
    retry_after_seconds: int | None = None


@dataclass
class ReadReceipt:
    """Квитанция прочтения исходящих клиентом (канало-нейтрально).

    ``up_to_external_id`` — числовой курсор: прочитано всё с числовым
    ``external_message_id <=`` него (TG: max_id из UpdateReadHistoryOutbox;
    сравнение числом, не лексически). ``None`` = канал дал только факт
    «чат прочитан» (MAX op_130 id сообщения не несёт) — прочитаны все
    исходящие диалога. Провайдер уже отделил self-прочтения менеджера
    и setAsUnread. ``read_at`` — только логи/форензика (mark у MAX), не
    персистится: у TG событие времени не несёт.
    """

    messenger: Messenger
    external_chat_id: str
    up_to_external_id: int | None = None
    read_at: datetime | None = None
