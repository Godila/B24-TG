"""CrmSyncWorker — воркер очереди CRM-синхронизации сообщений (план 006).

Outbox закрывает доставку в Telegram (5 попыток, backoff, flood-wait);
эта очередь закрывает доставку в Bitrix24 CRM: входящее сообщение
(контакт/сделка/timeline-комментарий/уведомление) и исходящее
(timeline-комментарий) обрабатываются НЕ в пути входящего события, а здесь
— с ретраями. Любая ошибка B24 (rate-limit ~2 rps на free-портале, сеть)
больше не теряет CRM-запись навсегда.

Механика — уменьшенная копия OutboxWorker: poll → попытка → backoff.
  - успех                -> применить результаты CRM к нашей БД, mark_done
  - исключение           -> attempts+1, backoff 30 * 2^attempts
  - attempts >= max      -> mark_failed (терминально, ERROR в лог)

``CrmSyncRepository`` — абстракция доступа к данным (fetch_due/mark_done/
mark_failed/reschedule/enqueue + сборка данных сообщения и запись
результатов); SQLAlchemy-реализация — в ``crm_sync_repo.py``.
"""

import asyncio
import base64
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from app.b24.sync import CRM_MODE_DEFAULT, Bitrix24Sync
from app.models import KIND_INBOUND, CrmSyncItem, Messenger

if TYPE_CHECKING:  # pragma: no cover - только для type-checker
    from app.media.storage import MediaStorage

logger = logging.getLogger(__name__)

#: Значение по умолчанию для лимита файла в timeline-комментарий B24
#: (переопределяется настройкой media_timeline_max_bytes из config).
MEDIA_TIMELINE_MAX_BYTES_DEFAULT = 5 * 1024 * 1024


@dataclass(slots=True)
class AttachmentMeta:
    """Метаданные вложения сообщения (файл читает воркер из медиа-тома)."""

    file_path: str  # относительный путь в медиа-томе
    file_name: str | None = None
    mime_type: str | None = None
    size: int | None = None


@dataclass(slots=True)
class CrmSyncData:
    """Данные сообщения (Message+Dialog+Contact+Manager), нужные воркеру."""

    message_text: str | None
    sender_name: str | None  # Contact.name (для входящих)
    sender_phone: str | None  # Contact.phone (для входящих)
    crm_contact_id: int | None  # Contact.crm_contact_id (для исходящих)
    crm_entity_id: int | None  # Dialog.crm_deal_id (id сущности из crm_entity_type)
    crm_entity_type: str | None  # Dialog.crm_entity_type ('deal'|'lead')
    # Ответственный диалога (None у общего номера) → ASSIGNED_BY_ID CRM.
    assigned_b24_user_id: int | None
    #: Кому в B24-чат падает уведомление о новом клиенте: ответственному,
    #: а у общего номера — всем активным участникам линии.
    notify_user_ids: list[int]
    messenger: Messenger  # канал диалога (тексты/источник CRM)
    sender_first_name: str | None = None  # Contact.first_name (split для CRM NAME)
    sender_last_name: str | None = None  # Contact.last_name (split для CRM LAST_NAME)
    sender_username: str | None = None  # Contact.username → IM-поле CRM
    attachments: list[AttachmentMeta] | None = None  # вложения сообщения


class CrmSyncRepository:
    """Абстракция доступа к очереди crm_sync (+ данные сообщения)."""

    async def fetch_due(self, limit: int = 20) -> list[CrmSyncItem]: ...

    async def mark_done(self, item: CrmSyncItem) -> None: ...

    async def mark_failed(self, item: CrmSyncItem, error: str) -> None: ...

    async def reschedule(
        self, item: CrmSyncItem, *, delay_seconds: int, error: str | None = None
    ) -> None: ...

    async def enqueue(self, *, kind: str, message_id: int) -> CrmSyncItem: ...

    async def collect(self, message_id: int) -> CrmSyncData | None:
        """Собрать данные для синхронизации; None — сообщение не найдено."""

    async def apply_inbound_result(
        self,
        message_id: int,
        *,
        contact_id: int | None,
        crm_entity_type: str | None,
        crm_entity_id: int | None,
        timeline_comment_id: int | None,
    ) -> None:
        """Записать SyncResult в нашу БД (Message/Contact/Dialog)."""

    async def set_timeline_comment(self, message_id: int, comment_id: int) -> None:
        """Записать timeline_comment_id исходящего сообщения."""

    async def get_timeline_mode(self) -> str:
        """Режим дублирования в таймлайн (app_settings.timeline_mode).

        Дефолт "all" — чтобы фейки в тестах наследовали прежнее поведение;
        SQLAlchemy-реализация читает настройку с дефолтом TIMELINE_MODE_DEFAULT.
        """
        return "all"

    async def get_media_to_timeline(self) -> bool:
        """Грузить ли файлы вложений в timeline-комментарии (default: нет —
        фейки в тестах наследуют прежнее текст-меточное поведение)."""
        return False

    async def get_crm_mode(self) -> str:
        """Какие карточки заводить новым клиентам (app_settings.crm_mode).

        Дефолт CRM_MODE_DEFAULT — фейки в тестах наследуют режим 'deal'."""
        return CRM_MODE_DEFAULT


class CrmSyncWorker:
    """Воркер очереди crm_sync: poll → CRM-вызовы → retry/backoff."""

    def __init__(
        self,
        repo: CrmSyncRepository,
        b24sync: Bitrix24Sync,
        max_attempts: int = 5,
        poll_interval: float = 2,
        batch_size: int = 20,
        media_storage: "MediaStorage | None" = None,
        media_timeline_max_bytes: int = MEDIA_TIMELINE_MAX_BYTES_DEFAULT,
    ):
        self._repo = repo
        self._b24sync = b24sync
        self._max_attempts = max_attempts
        self._poll_interval = poll_interval
        self._batch_size = batch_size
        # Медиа-том (bridge): чтение файлов вложений для timeline-комментариев.
        self._media_storage = media_storage
        self._media_timeline_max_bytes = media_timeline_max_bytes
        self._running = False

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def run(self) -> None:
        """Главный цикл: process → sleep → process → ... До ``stop()``."""
        self._running = True
        try:
            while self._running:
                try:
                    await self._process_once()
                except Exception:  # pragma: no cover - защитная сетка
                    logger.exception("CrmSyncWorker iteration failed; continuing")
                await asyncio.sleep(self._poll_interval)
        finally:
            self._running = False

    def stop(self) -> None:
        """Запросить остановку главного цикла (idempotent)."""
        self._running = False

    # ------------------------------------------------------------------ #
    # Single iteration
    # ------------------------------------------------------------------ #
    async def _process_once(self) -> None:
        # Режимы читаем раз на батч (настройки меняются редко).
        timeline_mode = await self._repo.get_timeline_mode()
        media_to_timeline = await self._repo.get_media_to_timeline()
        crm_mode = await self._repo.get_crm_mode()
        items = await self._repo.fetch_due(self._batch_size)
        for item in items:
            if item.kind == KIND_INBOUND:
                await self._handle_inbound(item, timeline_mode, media_to_timeline, crm_mode)
            else:
                await self._handle_outbound(item, timeline_mode, media_to_timeline)

    def _timeline_files(self, data: CrmSyncData, enabled: bool) -> list[tuple[str, str]]:
        """Вложения → [(имя, base64)] для FILES timeline-комментария B24.

        Настройка выключена / том не смонтирован / файла нет / превышен
        лимит — вложение молча остаётся текст-меткой («[фото]»): комментарий
        важнее файла, сбой загрузки не должен ронять CRM-запись.
        """
        if not enabled or self._media_storage is None or not data.attachments:
            return []
        from app.media.storage import MediaPathError

        files: list[tuple[str, str]] = []
        for att in data.attachments:
            if att.size is not None and att.size > self._media_timeline_max_bytes:
                logger.info(
                    "timeline: skip attachment %s — size %s > limit %s",
                    att.file_path,
                    att.size,
                    self._media_timeline_max_bytes,
                )
                continue
            try:
                path = self._media_storage.abs_path(att.file_path)
                if not path.is_file():
                    logger.warning("timeline: attachment file missing: %s", att.file_path)
                    continue
                content = path.read_bytes()
            except (MediaPathError, OSError):
                logger.warning("timeline: attachment unreadable: %s", att.file_path)
                continue
            if len(content) > self._media_timeline_max_bytes:
                logger.info("timeline: skip attachment %s — actual size > limit", att.file_path)
                continue
            name = att.file_name or Path(att.file_path).name
            files.append((name, base64.b64encode(content).decode("ascii")))
        return files

    async def _handle_inbound(
        self, item: CrmSyncItem, timeline_mode: str, media_to_timeline: bool, crm_mode: str
    ) -> None:
        data = await self._repo.collect(item.message_id)
        if data is None:
            # Сообщение удалено/не найдено — ретраи бессмысленны.
            logger.warning(
                "crm_sync item %s: message_id=%s not found — terminal fail",
                item.id,
                item.message_id,
            )
            await self._repo.mark_failed(item, "message_not_found")
            return

        try:
            result = await self._b24sync.process_inbound(
                sender_name=data.sender_name or "",
                sender_phone=data.sender_phone or "",
                message_text=data.message_text or "",
                assigned_b24_user_id=data.assigned_b24_user_id,
                notify_user_ids=data.notify_user_ids,
                messenger=data.messenger,
                existing_contact_id=data.crm_contact_id,
                existing_entity_id=data.crm_entity_id,
                existing_entity_type=data.crm_entity_type,
                crm_mode=crm_mode,
                timeline_mode=timeline_mode,
                sender_first_name=data.sender_first_name,
                sender_last_name=data.sender_last_name,
                sender_username=data.sender_username,
                files=self._timeline_files(data, media_to_timeline),
            )
            if result is None:
                # Нет B24-токена (интеграция не установлена) — ретраибельно:
                # счёт попыток с backoff, терминальный failed сделает
                # потерю видимой (а не молчаливой, как раньше).
                raise RuntimeError("no_b24_token")
            await self._repo.apply_inbound_result(
                item.message_id,
                contact_id=result.contact_id,
                crm_entity_type=result.crm_entity_type,
                crm_entity_id=result.crm_entity_id,
                timeline_comment_id=result.timeline_comment_id,
            )
        except Exception as exc:  # noqa: BLE001 — очередь обязана пережить любой сбой B24
            await self._fail_or_retry(item, exc)
            return

        await self._repo.mark_done(item)

    async def _handle_outbound(
        self, item: CrmSyncItem, timeline_mode: str, media_to_timeline: bool
    ) -> None:
        data = await self._repo.collect(item.message_id)
        if data is None:
            logger.warning(
                "crm_sync item %s: message_id=%s not found — terminal fail",
                item.id,
                item.message_id,
            )
            await self._repo.mark_failed(item, "message_not_found")
            return

        try:
            comment_id = await self._b24sync.process_outbound(
                dialog_entity_id=data.crm_entity_id,
                dialog_entity_type=data.crm_entity_type,
                contact_id=data.crm_contact_id,
                text=data.message_text or "",
                timeline_mode=timeline_mode,
                files=self._timeline_files(data, media_to_timeline),
            )
            if comment_id is not None:
                await self._repo.set_timeline_comment(item.message_id, comment_id)
            # comment_id=None — писать некуда (нет сделки и контакта) или нет
            # токена; для исходящих это не ошибка, терминальный done.
        except Exception as exc:  # noqa: BLE001 — очередь обязана пережить любой сбой B24
            await self._fail_or_retry(item, exc)
            return

        await self._repo.mark_done(item)

    async def _fail_or_retry(self, item: CrmSyncItem, exc: Exception) -> None:
        """Попытка не удалась: backoff 30 * 2^attempts, после max — failed."""
        error = str(exc) or exc.__class__.__name__
        if item.attempts + 1 >= self._max_attempts:
            logger.error(
                "crm_sync item %s (kind=%s, message_id=%s) failed permanently: %s",
                item.id,
                item.kind,
                item.message_id,
                error,
            )
            await self._repo.mark_failed(item, error)
            return
        delay = 30 * (2**item.attempts)
        # Инцидент 2026-08-17 (LAST_NAME=null): сбои были тихи в логах —
        # только last_error в БД; диагностировали по cadence запросов.
        logger.warning(
            "crm_sync item %s (kind=%s, message_id=%s) attempt %s/%s failed, retry через %ss: %s",
            item.id,
            item.kind,
            item.message_id,
            item.attempts + 1,
            self._max_attempts,
            delay,
            error,
        )
        await self._repo.reschedule(item, delay_seconds=delay, error=error)


# Тайпинг колбэка постановки в очередь (IncomingHandler / outbox-hook).
CrmSyncEnqueue = Callable[..., Awaitable[None]]
