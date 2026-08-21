from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from pathlib import Path

from app.messaging.resolve import ParsedDest, ResolvedPeer
from app.messaging.types import ContentType, IncomingMessage, ReadReceipt, SendResult


class SessionRevokedError(Exception):
    """Сессия устройства отозвана (логаут/инвалидация) — ретраи бессмысленны.

    Канально-нейтральный терминальный сигнал: TelegramProvider поднимает его
    при неавторизованной .session, MaxAuthError у MAX — тот же смысл.
    AccountSyncWorker по нему переводит аккаунт в offline и алертит
    «переподключите по QR», вместо бесконечных ретраев «сетевого сбоя»."""


class MessengerProvider(ABC):
    """Абстракция над мессенджером.

    Один экземпляр = один канальный аккаунт (один менеджер). Точка
    расширения: TelegramProvider (Telethon/MTProto), MaxUserProvider
    (WebSocket web-клиента MAX).

    Read-квитанции исходящих (read) — ОПЦИОНАЛЬНАЯ возможность канала:
    ``read_receipt_stream`` ниже. Статусы delivered в контракт не входят:
    user-API обоих каналов их не дают, outbox закрывает цикл исходящих
    через mark_sent.
    """

    @abstractmethod
    async def connect(self) -> None:
        """Подключиться / авторизоваться. Raise = аккаунт не зарегистрирован."""

    @abstractmethod
    async def disconnect(self) -> None:
        """Корректно закрыть соединение (идемпотентно)."""

    @abstractmethod
    def is_connected(self) -> bool:
        """Живо ли соединение прямо сейчас (для HealthChecker)."""

    @abstractmethod
    def incoming_stream(self) -> AsyncIterator[IncomingMessage]:
        """Асинхронный поток входящих сообщений.

        Несёт и device-outbound (``direction=outbound``): сообщения менеджера,
        отправленные с устройства, — провайдер уже отделил их от эха
        собственных отправок. Реализации Telethon-типа держат соединение сами
        (реконнект внутри провайдера); поток живёт, пока не вызван disconnect().
        """

    @abstractmethod
    async def send_message(
        self, external_chat_id: str, text: str, *, is_initiation: bool
    ) -> SendResult:
        """Отправить сообщение. is_initiation влияет на throttle."""

    # --- Read-квитанции исходящих: ОПЦИОНАЛЬНАЯ возможность канала ---
    # --- (прецедент — supports_media): дефолт «пустой поток».       ---
    async def read_receipt_stream(self) -> AsyncIterator[ReadReceipt]:
        """Асинхронный поток прочтений наших исходящих клиентом.

        Каналы без read-квитанций наследуют пустой дефолт — потребитель
        просто не получает событий. MAX завершает поток сентинелом при
        disconnect() (как incoming_stream).
        """
        return
        yield  # превращает метод в async-генератор (дефолт пуст)

    # --- Медиа: ОПЦИОНАЛЬНАЯ возможность канала (прецедент — ---
    # --- is_dead/credential_token): дефолт «не поддержано».   ---
    def supports_media(self) -> bool:
        """Может ли канал отправлять файлы (гейт API + защита воркера)."""
        return False

    async def send_media(
        self,
        external_chat_id: str,
        path: Path,
        content_type: ContentType,
        *,
        mime_type: str | None = None,
        file_name: str | None = None,
        caption: str | None = None,
        is_initiation: bool = False,
    ) -> SendResult:
        """Отправить медиа-файл с общего тома. Канал без поддержки медиа
        (провайдер без MediaStorage) наследует этот дефолт — элемент честно
        падает в failed."""
        return SendResult(success=False, error="media_not_supported")

    def is_dead(self) -> bool:
        """Сессия отозвана безнадёжно (токен MAX сброшен) — реконнект
        бессмыслен, провайдера надо снять. TG: всегда False."""
        return False

    def credential_token(self) -> str | None:
        """Кред-токен живой сессии провайдера; расхождение с токеном строки
        аккаунта означает перепривязку (новый QR) — провайдер устарел.
        TG: None (сессия в файле, перепривязку отслеживает путь файла)."""
        return None

    # --- Резолв «написать первым»: ОПЦИОНАЛЬНАЯ возможность канала ---
    async def resolve_peer(self, dest: ParsedDest) -> ResolvedPeer | None:
        """Резолв телефона/@username → peer. None = не найден или скрыт
        настройками приватности (терминально); исключение = сбой канала.
        Каналы без поиска наследуют дефолт NotImplementedError."""
        raise NotImplementedError(f"resolve_peer не поддержан: {dest.kind}")
