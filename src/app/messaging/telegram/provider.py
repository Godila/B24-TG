import asyncio
import logging
from collections.abc import AsyncIterator
from pathlib import Path

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from telethon.tl import types as tl
from telethon.tl.types import User

from app.messaging.provider import MessengerProvider, SessionRevokedError
from app.messaging.types import ContentType, IncomingMessage, SendResult
from app.models import Messenger

logger = logging.getLogger(__name__)


class TelegramProvider(MessengerProvider):
    """Реализация MessengerProvider поверх Telethon (MTProto user-API).
    Один экземпляр = одна TG-сессия (один менеджер)."""

    def __init__(self, api_id: int, api_hash: str, sessions_dir: str | Path,
                 proxy: tuple | None = None):
        self._api_id = api_id
        self._api_hash = api_hash
        self._sessions_dir = Path(sessions_dir)
        self._sessions_dir.mkdir(parents=True, exist_ok=True)
        self._proxy = proxy
        self._client: TelegramClient | None = None
        self._incoming_queue: asyncio.Queue[IncomingMessage] = asyncio.Queue()

    @property
    def session_file(self) -> Path:
        return self._sessions_dir / "session"

    async def connect(self) -> None:
        self._client = TelegramClient(
            str(self.session_file), self._api_id, self._api_hash,
            proxy=self._proxy,
            # CRITICAL: авто-реконнект Telethon на «TCP жив, сервер рвёт
            # после рукопожатия» молотит реконнектами БЕЗ задержки и БЕЗ
            # лимита (sleep в _reconnect только на ошибке коннекта, которой
            # нет; retries/retry_delay этот путь не ограничивают) — сутки
            # дауна туннеля = сотни тысяч соединений в панель. Вместо
            # бесконечного авто-реконнекта — быстрый чистый отказ, а
            # повторами владеет AccountSyncWorker (грейс + свой каденс).
            auto_reconnect=False,
        )
        await self._client.connect()
        if not await self._client.is_user_authorized():
            # .session инвалидирована (логаут устройства/смена номера) —
            # терминально: AccountSyncWorker переведёт аккаунт в offline и
            # алертит «переподключите по QR», без бесконечных ретраев.
            # Транспорт уже поднят — закрываем, иначе соединение-зомби.
            await self.disconnect()
            raise SessionRevokedError("TG session not authorized — нужен QR-онбординг")
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

    async def log_out(self) -> None:
        """Отвязка аккаунта: RPC log_out + удаление .session-файла.

        После этого провайдер непригоден — bridge снимает его
        (AccountSyncWorker.force_unregister)."""
        if self._client:
            await self._client.log_out()
            self._client = None

    def is_connected(self) -> bool:
        return bool(
            self._client and self._client.is_connected()
        )

    async def _on_new_message(self, event) -> None:
        """Handler событий Telethon NewMessage — кладёт в очередь."""
        try:
            sender = await event.get_sender()
            ctype, text = self._content_type_and_text(event.message)
            msg = IncomingMessage(
                messenger=Messenger.tg,
                external_chat_id=str(event.chat_id),
                sender_external_id=str(getattr(sender, "id", 0)),
                sender_name=self._full_name(sender),
                sender_phone=getattr(sender, "phone", None),
                sender_username=getattr(sender, "username", None),
                content_type=ctype,
                text=text,
                external_message_id=str(event.message.id),
                timestamp=event.message.date,
                is_reply=bool(event.is_reply),
            )
            await self._incoming_queue.put(msg)
        except Exception:
            logger.exception("Failed to handle incoming TG message")

    @staticmethod
    def _content_type_and_text(message) -> tuple[ContentType, str | None]:
        """Тип контента и текст сообщения TG.

        У медиа-сообщений ``message.message`` — это подпись (caption), она может
        быть пустой; вместо молчаливой потери подставляем плейсхолдер, чтобы
        сообщение не превращалось в пустой пузырь в чате и пустой коммент в CRM.
        """
        text = message.message or None
        media = getattr(message, "media", None)
        if media is None:
            return ContentType.text, text
        if isinstance(media, tl.MessageMediaPhoto):
            return ContentType.photo, text or "[фото]"
        if isinstance(media, tl.MessageMediaDocument):
            attrs = getattr(media.document, "attributes", [])
            names = {type(a).__name__ for a in attrs}
            if "DocumentAttributeAudio" in names:
                return ContentType.voice, text or "[голосовое сообщение]"
            if "DocumentAttributeVideo" in names:
                return ContentType.video, text or "[видео]"
            if "DocumentAttributeSticker" in names:
                return ContentType.sticker, text or "[стикер]"
            return ContentType.file, text or "[файл]"
        return ContentType.file, text or "[вложение]"

    @staticmethod
    def _full_name(sender) -> str | None:
        if not isinstance(sender, User):
            return None
        parts = [p for p in (sender.first_name, sender.last_name) if p]
        return " ".join(parts) or None

    async def incoming_stream(self) -> AsyncIterator[IncomingMessage]:
        while True:
            yield await self._incoming_queue.get()

    async def send_message(
        self, external_chat_id: str, text: str, *, is_initiation: bool
    ) -> SendResult:
        if not self._client:
            return SendResult(success=False, error="not connected")
        try:
            result = await self._client.send_message(int(external_chat_id), text)
            return SendResult(success=True, external_message_id=str(result.id))
        except FloodWaitError as e:
            return SendResult(
                success=False,
                error="flood_wait",
                retry_after_seconds=int(e.seconds),
            )
        except Exception as e:
            logger.exception("send_message failed")
            return SendResult(success=False, error=str(e))
