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
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.b24.sync import Bitrix24Sync
from app.models import KIND_INBOUND, CrmSyncItem, Messenger

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class CrmSyncData:
    """Данные сообщения (Message+Dialog+Contact+Manager), нужные воркеру."""

    message_text: str | None
    sender_name: str | None        # Contact.name (для входящих)
    sender_phone: str | None       # Contact.phone (для входящих)
    crm_contact_id: int | None     # Contact.crm_contact_id (для исходящих)
    crm_deal_id: int | None        # Dialog.crm_deal_id
    crm_entity_type: str | None    # Dialog.crm_entity_type
    assigned_b24_user_id: int | None  # Manager.b24_user_id диалога
    messenger: Messenger           # канал диалога (тексты/источник CRM)


class CrmSyncRepository:
    """Абстракция доступа к очереди crm_sync (+ данные сообщения)."""

    async def fetch_due(self, limit: int = 20) -> list[CrmSyncItem]:
        ...

    async def mark_done(self, item: CrmSyncItem) -> None:
        ...

    async def mark_failed(self, item: CrmSyncItem, error: str) -> None:
        ...

    async def reschedule(
        self, item: CrmSyncItem, *, delay_seconds: int, error: str | None = None
    ) -> None:
        ...

    async def enqueue(self, *, kind: str, message_id: int) -> CrmSyncItem:
        ...

    async def collect(self, message_id: int) -> CrmSyncData | None:
        """Собрать данные для синхронизации; None — сообщение не найдено."""

    async def apply_inbound_result(
        self,
        message_id: int,
        *,
        contact_id: int | None,
        deal_id: int | None,
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


class CrmSyncWorker:
    """Воркер очереди crm_sync: poll → CRM-вызовы → retry/backoff."""

    def __init__(
        self,
        repo: CrmSyncRepository,
        b24sync: Bitrix24Sync,
        max_attempts: int = 5,
        poll_interval: float = 2,
        batch_size: int = 20,
    ):
        self._repo = repo
        self._b24sync = b24sync
        self._max_attempts = max_attempts
        self._poll_interval = poll_interval
        self._batch_size = batch_size
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
        # Режим таймлайна читаем раз на батч (сеттинг меняется редко).
        timeline_mode = await self._repo.get_timeline_mode()
        items = await self._repo.fetch_due(self._batch_size)
        for item in items:
            if item.kind == KIND_INBOUND:
                await self._handle_inbound(item, timeline_mode)
            else:
                await self._handle_outbound(item, timeline_mode)

    async def _handle_inbound(self, item: CrmSyncItem, timeline_mode: str) -> None:
        data = await self._repo.collect(item.message_id)
        if data is None:
            # Сообщение удалено/не найдено — ретраи бессмысленны.
            logger.warning(
                "crm_sync item %s: message_id=%s not found — terminal fail",
                item.id, item.message_id,
            )
            await self._repo.mark_failed(item, "message_not_found")
            return
        if data.assigned_b24_user_id is None:
            # Без ответственного менеджера process_inbound вызвать нельзя;
            # менеджер не появится сам — терминально.
            logger.warning(
                "crm_sync item %s: message_id=%s has no assigned manager — "
                "terminal fail",
                item.id, item.message_id,
            )
            await self._repo.mark_failed(item, "no_assigned_manager")
            return

        try:
            result = await self._b24sync.process_inbound(
                sender_name=data.sender_name or "",
                sender_phone=data.sender_phone or "",
                message_text=data.message_text or "",
                assigned_b24_user_id=data.assigned_b24_user_id,
                messenger=data.messenger,
                existing_contact_id=data.crm_contact_id,
                existing_deal_id=data.crm_deal_id,
                timeline_mode=timeline_mode,
            )
            if result is None:
                # Нет B24-токена (интеграция не установлена) — ретраибельно:
                # счёт попыток с backoff, терминальный failed сделает
                # потерю видимой (а не молчаливой, как раньше).
                raise RuntimeError("no_b24_token")
            await self._repo.apply_inbound_result(
                item.message_id,
                contact_id=result.contact_id,
                deal_id=result.deal_id,
                timeline_comment_id=result.timeline_comment_id,
            )
        except Exception as exc:  # noqa: BLE001 — очередь обязана пережить любой сбой B24
            await self._fail_or_retry(item, exc)
            return

        await self._repo.mark_done(item)

    async def _handle_outbound(self, item: CrmSyncItem, timeline_mode: str) -> None:
        data = await self._repo.collect(item.message_id)
        if data is None:
            logger.warning(
                "crm_sync item %s: message_id=%s not found — terminal fail",
                item.id, item.message_id,
            )
            await self._repo.mark_failed(item, "message_not_found")
            return

        try:
            comment_id = await self._b24sync.process_outbound(
                dialog_deal_id=data.crm_deal_id,
                dialog_entity_type=data.crm_entity_type,
                contact_id=data.crm_contact_id,
                text=data.message_text or "",
                timeline_mode=timeline_mode,
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
                item.id, item.kind, item.message_id, error,
            )
            await self._repo.mark_failed(item, error)
            return
        delay = 30 * (2 ** item.attempts)
        await self._repo.reschedule(item, delay_seconds=delay, error=error)


# Тайпинг колбэка постановки в очередь (IncomingHandler / outbox-hook).
CrmSyncEnqueue = Callable[..., Awaitable[None]]
