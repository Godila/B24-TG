import asyncio
import logging
from collections.abc import AsyncIterator
from pathlib import Path

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from telethon.tl import types as tl
from telethon.tl.types import User

from app.media.storage import MediaStorage, ext_for, sanitize_file_name
from app.messaging.provider import MessengerProvider, SessionRevokedError
from app.messaging.types import (
    MEDIA_PLACEHOLDERS,
    ContentType,
    IncomingMessage,
    MediaPayload,
    ReadReceipt,
    SendResult,
)
from app.models import MessageDirection, Messenger

logger = logging.getLogger(__name__)


class TelegramProvider(MessengerProvider):
    """Реализация MessengerProvider поверх Telethon (MTProto user-API).
    Один экземпляр = одна TG-сессия (один менеджер)."""

    def __init__(
        self,
        api_id: int,
        api_hash: str,
        sessions_dir: str | Path,
        proxy: tuple | None = None,
        *,
        media_storage: MediaStorage | None = None,
        media_download_timeout_sec: float = 120.0,
        media_send_timeout_sec: float = 300.0,
    ):
        self._api_id = api_id
        self._api_hash = api_hash
        self._sessions_dir = Path(sessions_dir)
        self._sessions_dir.mkdir(parents=True, exist_ok=True)
        self._proxy = proxy
        self._client: TelegramClient | None = None
        self._incoming_queue: asyncio.Queue[IncomingMessage] = asyncio.Queue()
        # Read-квитанции исходящих (UpdateReadHistoryOutbox).
        self._read_queue: asyncio.Queue[ReadReceipt] = asyncio.Queue()
        # None = входящие медиа не скачиваются (тесты/онбординг) — в тексте
        # останутся плейсхолдеры «[фото]» (прежнее поведение).
        self._media = media_storage
        self._media_download_timeout = media_download_timeout_sec
        self._media_send_timeout = media_send_timeout_sec

    @property
    def session_file(self) -> Path:
        return self._sessions_dir / "session"

    async def connect(self) -> None:
        self._client = TelegramClient(
            str(self.session_file),
            self._api_id,
            self._api_hash,
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
        self._client.add_event_handler(self._on_new_message, events.NewMessage(incoming=True))
        # Исходящие с ДРУГИХ устройств аккаунта (телефон менеджера) —
        # device-outbound инжест. Эхо наших send_* не приходит вовсе:
        # Telethon не диспетчит RPC-результаты этой сессии в события.
        self._client.add_event_handler(self._on_device_outbound, events.NewMessage(outgoing=True))
        # Read-квитанции исходящих: inbox=False (дефолт builder'а) пропускает
        # только UpdateReadHistoryOutbox — «наши сообщения прочитаны клиентом»;
        # собственные прочтения входящих отфильтрованы самим builder'ом.
        self._client.add_event_handler(self._on_outbox_read, events.MessageRead())
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
        return bool(self._client and self._client.is_connected())

    async def _on_new_message(self, event) -> None:
        """Входящие (клиент → менеджер) — горячий путь, не менялся."""
        await self._handle_new_message(event, direction=MessageDirection.inbound)

    async def _on_device_outbound(self, event) -> None:
        """Исходящие с устройства менеджера (не из виджета).

        Сюда доходят только события с других авторизаций аккаунта (телефон).
        Saved Messages (чат с самим собой) отдельного гарда не требует:
        диалога с собственным id не существует — IncomingHandler скипнет его
        по правилу «device-outbound только в существующие диалоги».
        """
        await self._handle_new_message(event, direction=MessageDirection.outbound)

    async def _handle_new_message(self, event, *, direction: MessageDirection) -> None:
        """Общая обработка NewMessage-события обоих направлений."""
        try:
            # Только приватные диалоги: без этого фильтра сообщения
            # групп/каналов инжестятся как «клиенты» (контакт+сделка в CRM,
            # ответ менеджера уйдёт в группу). Действует в оба направления
            # (менеджер может писать и в группы). MAX-провайдер фильтрует
            # аналогично (_chat_is_dialog).
            if not event.is_private:
                logger.debug("TG: скип группового/канального сообщения chat=%s", event.chat_id)
                return
            if direction is MessageDirection.outbound:
                # Отправитель — сам менеджер: контактные поля не значимы,
                # обработчик берёт клиента из существующего диалога.
                sender = None
                sender_external_id = str(event.sender_id or 0)
            else:
                sender = await event.get_sender()
                sender_external_id = str(getattr(sender, "id", 0))
            ctype, text = self._content_type_and_text(event.message)
            media = None
            # Стикеры не качаем: анимированные .tgs/.webp не рендерятся
            # браузером — плейсхолдер честнее битой картинки.
            if ctype not in (ContentType.text, ContentType.sticker):
                # Eager: file_reference медиа живёт минуты — между событием
                # и обработкой очередь может подваить, ленивая догрузка
                # умерла бы. Сбой скачивания ≠ потеря сообщения (None →
                # плейсхолдер в тексте).
                media = await self._download_media(
                    event.message,
                    direction="out" if direction is MessageDirection.outbound else "in",
                )
            msg = IncomingMessage(
                messenger=Messenger.tg,
                external_chat_id=str(event.chat_id),
                sender_external_id=sender_external_id,
                sender_name=self._full_name(sender),
                sender_phone=getattr(sender, "phone", None),
                sender_username=getattr(sender, "username", None),
                sender_first_name=getattr(sender, "first_name", None),
                sender_last_name=getattr(sender, "last_name", None),
                content_type=ctype,
                text=text,
                media=media,
                external_message_id=str(event.message.id),
                timestamp=event.message.date,
                is_reply=bool(event.is_reply),
                direction=direction,
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
            return ContentType.photo, text or MEDIA_PLACEHOLDERS[ContentType.photo]
        if isinstance(media, tl.MessageMediaDocument):
            attrs = getattr(media.document, "attributes", [])
            names = {type(a).__name__ for a in attrs}
            if "DocumentAttributeAudio" in names:
                return ContentType.voice, text or MEDIA_PLACEHOLDERS[ContentType.voice]
            if "DocumentAttributeVideo" in names:
                return ContentType.video, text or MEDIA_PLACEHOLDERS[ContentType.video]
            if "DocumentAttributeSticker" in names:
                return ContentType.sticker, text or MEDIA_PLACEHOLDERS[ContentType.sticker]
            return ContentType.file, text or MEDIA_PLACEHOLDERS[ContentType.file]
        return ContentType.file, text or "[вложение]"

    @staticmethod
    def _media_meta(message) -> tuple[str | None, int | None, str | None]:
        """(mime, size, file_name) медиа-сообщения TG.

        Photo не несёт ни mime, ни имени — JPG по факту формата TG.
        """
        media = getattr(message, "media", None)
        if isinstance(media, tl.MessageMediaPhoto):
            return "image/jpeg", None, None
        if isinstance(media, tl.MessageMediaDocument):
            doc = getattr(media, "document", None)
            if doc is None:
                return None, None, None
            file_name = None
            for attr in getattr(doc, "attributes", []):
                if isinstance(attr, tl.DocumentAttributeFilename):
                    file_name = sanitize_file_name(attr.file_name)
                    break
            # mime задаёт клиент отправителя и длиной не ограничен; колонка
            # БД — String(128), длинный mime уронил бы всё сообщение.
            mime = getattr(doc, "mime_type", None)
            if mime:
                mime = mime[:128]
            return mime, getattr(doc, "size", None), file_name
        return None, None, None

    async def _download_media(self, message, *, direction: str = "in") -> MediaPayload | None:
        """Скачать медиа на общий том; None = не качаем (лимит/сбой).

        ``direction`` — папка тома: "in" (входящие) или "out" (медиа
        device-outbound сообщений менеджера).
        В БД кладём путь, куда Telethon РЕАЛЬНО записал файл (result):
        при пути без расширения он дописывает своё (webpage-превью → .jpg,
        контакт → .vcard), при коллизии — суффикс « (1)». Запрошенный путь
        при этом остаётся враньём → вечный 404 раздачи + файл-сирота.
        """
        if self._media is None or self._client is None:
            return None
        mime, declared_size, file_name = self._media_meta(message)
        max_size = self._media.max_size_bytes
        if max_size is not None and declared_size is not None and declared_size > max_size:
            logger.warning("TG media skip: declared size %s > limit %s", declared_size, max_size)
            return None
        # new_path ВНУТРИ try: mkdir/resolve на недоступном томе — OSError,
        # снаружи она улетала в catch-all события и теряла ВСЁ сообщение,
        # ломая инвариант «сбой медиа ≠ потеря текста».
        absolute: Path | None = None
        try:
            absolute, _ = self._media.new_path(direction=direction, ext=ext_for(file_name, mime))
            result = await asyncio.wait_for(
                self._client.download_media(message, file=str(absolute)),
                timeout=self._media_download_timeout,
            )
            if not result:
                return None
            actual = Path(result)
            try:
                relative = actual.resolve().relative_to(self._media.root.resolve()).as_posix()
            except ValueError:
                logger.error("TG media written outside media root: %s", result)
                actual.unlink(missing_ok=True)
                return None
            actual_size = actual.stat().st_size
            if max_size is not None and actual_size > max_size:
                actual.unlink(missing_ok=True)
                logger.warning("TG media skip: actual size %s > limit", actual_size)
                return None
            return MediaPayload(
                path=relative, mime_type=mime, size=actual_size, file_name=file_name
            )
        except Exception:
            logger.exception("TG media download failed; message keeps placeholder")
            if absolute is not None:
                absolute.unlink(missing_ok=True)
            return None

    @staticmethod
    def _full_name(sender) -> str | None:
        if not isinstance(sender, User):
            return None
        parts = [p for p in (sender.first_name, sender.last_name) if p]
        return " ".join(parts) or None

    async def _on_outbox_read(self, event) -> None:
        """Клиент прочитал наши исходящие: прочитано всё с id <= max_id.

        Only-put (unbounded очередь — диспетчер Telethon не блокируется),
        БД трогает ReadMarker на стороне bridge.
        """
        try:
            if not event.outbox:
                return  # паритет фильтра builder'а (inbox-события)
            max_id = int(event.max_id or 0)
            if max_id <= 0:
                return  # contents-событие без курсора (прочтение войса)
            chat_id = event.chat_id
            if chat_id is None or chat_id < 0:
                # is_private у MessageRead нет: группы/каналы — marked id
                # отрицательный; приватный чат — положительный id клиента.
                # Диалога-группы в БД всё равно нет — двойная защита.
                return
            await self._read_queue.put(
                ReadReceipt(
                    messenger=Messenger.tg,
                    external_chat_id=str(chat_id),
                    up_to_external_id=max_id,
                )
            )
        except Exception:
            logger.exception("Failed to handle TG read event")

    async def incoming_stream(self) -> AsyncIterator[IncomingMessage]:
        while True:
            yield await self._incoming_queue.get()

    async def read_receipt_stream(self) -> AsyncIterator[ReadReceipt]:
        # Без сентинела: forward-таску гасит cancellation (как incoming).
        while True:
            yield await self._read_queue.get()

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

    def supports_media(self) -> bool:
        return True

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
        """Отправить файл (send_file): фото — как фото, аудио — voice-note.

        Расширение файла сохранено при записи на том — автодетект Telethon
        сам отправит изображение как photo, остальное документом. Имя
        документа в TG берётся из basename пути (у нас — uuid), поэтому
        оригинальное имя передаём атрибутом явно.
        """
        if not self._client:
            return SendResult(success=False, error="not connected")
        name = file_name or path.name
        try:
            result = await asyncio.wait_for(
                self._client.send_file(
                    int(external_chat_id),
                    str(path),
                    # Пустой caption не отправляем (пустой пузырь над файлом).
                    caption=caption or None,
                    voice_note=content_type == ContentType.voice,
                    attributes=[tl.DocumentAttributeFilename(file_name=name)],
                ),
                timeout=self._media_send_timeout,
            )
            return SendResult(success=True, external_message_id=str(result.id))
        except FloodWaitError as e:
            return SendResult(
                success=False,
                error="flood_wait",
                retry_after_seconds=int(e.seconds),
            )
        except TimeoutError:
            # Полумёртвый туннель: воркер обрабатывает элементы последовательно —
            # без таймаута одна висящая отправка остановила бы все исходящие.
            logger.error("send_media timeout chat=%s file=%s", external_chat_id, name)
            return SendResult(success=False, error="send_timeout")
        except Exception as e:
            logger.exception("send_media failed")
            return SendResult(success=False, error=str(e))
