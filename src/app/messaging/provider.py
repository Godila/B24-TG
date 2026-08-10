from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from app.messaging.types import DeliveryStatus, IncomingMessage, SendResult


class MessengerProvider(ABC):
    """Абстракция над мессенджером.

    Точка расширения: TelegramProvider (Фаза 1), MaxProvider (позже).
    """

    @abstractmethod
    async def connect(self) -> None:
        """Подключиться / авторизоваться."""

    @abstractmethod
    async def disconnect(self) -> None:
        """Корректно закрыть соединение."""

    @abstractmethod
    def incoming_stream(self) -> AsyncIterator[IncomingMessage]:
        """Асинхронный поток входящих сообщений."""

    @abstractmethod
    async def send_message(
        self, account_id: int, external_chat_id: str, text: str, *, is_initiation: bool
    ) -> SendResult:
        """Отправить сообщение. is_initiation влияет на throttle."""

    @abstractmethod
    def status_stream(self) -> AsyncIterator[tuple[int, DeliveryStatus]]:
        """Поток обновлений статусов доставки (message_id, status)."""
