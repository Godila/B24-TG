from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from app.messaging.types import IncomingMessage, SendResult


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

    Статусы доставки (delivered/read) в контракт не входят: outbox закрывает
    цикл исходящих через mark_sent, платформы дают разную глубину статусов.
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

        Реализации Telethon-типа держат соединение сами (реконнект внутри
        провайдера); поток живёт, пока не вызван disconnect().
        """

    @abstractmethod
    async def send_message(
        self, external_chat_id: str, text: str, *, is_initiation: bool
    ) -> SendResult:
        """Отправить сообщение. is_initiation влияет на throttle."""

    def is_dead(self) -> bool:
        """Сессия отозвана безнадёжно (токен MAX сброшен) — реконнект
        бессмыслен, провайдера надо снять. TG: всегда False."""
        return False

    def credential_token(self) -> str | None:
        """Кред-токен живой сессии провайдера; расхождение с токеном строки
        аккаунта означает перепривязку (новый QR) — провайдер устарел.
        TG: None (сессия в файле, перепривязку отслеживает путь файла)."""
        return None
