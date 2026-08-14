import asyncio
import logging
from collections.abc import AsyncIterator
from pathlib import Path

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from telethon.tl.types import User

from app.messaging.provider import MessengerProvider
from app.messaging.types import (
    ContentType,
    DeliveryStatus,
    IncomingMessage,
    SendResult,
)

logger = logging.getLogger(__name__)


class TelegramProvider(MessengerProvider):
    """Реализация MessengerProvider поверх Telethon (MTProto user-API).
    Один экземпляр = одна TG-сессия (один менеджер)."""

    def __init__(self, api_id: int, api_hash: str, sessions_dir: str):
        self._api_id = api_id
        self._api_hash = api_hash
        self._sessions_dir = Path(sessions_dir)
        self._sessions_dir.mkdir(parents=True, exist_ok=True)
        self._client: TelegramClient | None = None
        self._incoming_queue: asyncio.Queue[IncomingMessage] = asyncio.Queue()
        self._status_queue: asyncio.Queue[tuple[int, DeliveryStatus]] = asyncio.Queue()

    @property
    def session_file(self) -> Path:
        return self._sessions_dir / "session"

    async def connect(self) -> None:
        self._client = TelegramClient(
            str(self.session_file), self._api_id, self._api_hash
        )
        await self._client.connect()
        if not await self._client.is_user_authorized():
            raise RuntimeError("TG session not authorized — run auth_login first")
        # incoming=True: только входящие. Без builder Telethon отдаёт сырые Update,
        # а без фильтра исходящие сообщения менеджера эхом шли бы как входящие.
        self._client.add_event_handler(
            self._on_new_message, events.NewMessage(incoming=True)
        )
        logger.info("TelegramProvider connected")

    async def disconnect(self) -> None:
        if self._client:
            await self._client.disconnect()
            self._client = None

    async def _on_new_message(self, event) -> None:
        """Handler событий Telethon NewMessage — кладёт в очередь."""
        try:
            sender = await event.get_sender()
            msg = IncomingMessage(
                account_id=0,  # SessionManager проставит реальный account_id
                external_chat_id=str(event.chat_id),
                sender_tg_id=getattr(sender, "id", 0),
                sender_name=self._full_name(sender),
                sender_phone=getattr(sender, "phone", None),
                sender_username=getattr(sender, "username", None),
                content_type=ContentType.text,
                text=event.message.message,
                external_message_id=event.message.id,
                timestamp=event.message.date,
                is_reply=bool(event.is_reply),
            )
            await self._incoming_queue.put(msg)
        except Exception:
            logger.exception("Failed to handle incoming TG message")

    @staticmethod
    def _full_name(sender) -> str | None:
        if not isinstance(sender, User):
            return None
        parts = [p for p in (sender.first_name, sender.last_name) if p]
        return " ".join(parts) or None

    async def incoming_stream(self) -> AsyncIterator[IncomingMessage]:
        while True:
            yield await self._incoming_queue.get()

    async def status_stream(self) -> AsyncIterator[tuple[int, DeliveryStatus]]:
        while True:
            yield await self._status_queue.get()

    async def send_message(
        self, account_id: int, external_chat_id: str, text: str, *, is_initiation: bool
    ) -> SendResult:
        if not self._client:
            return SendResult(success=False, error="not connected")
        try:
            result = await self._client.send_message(int(external_chat_id), text)
            return SendResult(success=True, external_message_id=result.id)
        except FloodWaitError as e:
            return SendResult(
                success=False,
                error="flood_wait",
                flood_wait_seconds=int(e.seconds),
            )
        except Exception as e:
            logger.exception("send_message failed")
            return SendResult(success=False, error=str(e))
