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
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from app.b24.channels import channel_profile
from app.b24.client import Bitrix24Error
from app.b24.im import ImService
from app.b24.notify import (
    build_keyboard,
    build_notification_text,
    crm_card_url,
    dismiss_command_button,
    dismiss_link_button,
    sign_dismiss_url,
)
from app.b24.openlines import (
    OpenLineService,
    build_delivery_message,
    build_send_message,
    to_unixsec,
)
from app.b24.sync import CRM_MODE_DEFAULT, Bitrix24Sync
from app.b24.token_manager import TokenManager
from app.config import get_settings
from app.media.public_url import sign_media_url
from app.models import (
    KIND_INBOUND,
    KIND_NOTIFY,
    CrmSyncItem,
    DialogNotification,
    Messenger,
)

if TYPE_CHECKING:  # pragma: no cover - только для type-checker
    from app.media.storage import MediaStorage

logger = logging.getLogger(__name__)

#: Значение по умолчанию для лимита файла в timeline-комментарий B24
#: (переопределяется настройкой media_timeline_max_bytes из config).
MEDIA_TIMELINE_MAX_BYTES_DEFAULT = 5 * 1024 * 1024

#: Коды im.message.delete, на которых деградируем (сообщение осталось в
#: чате, состояние нашей БД консистентно): не найдено (уже удалено) и
#: истёкшее окно правки. Прочие Bitrix24Error (502→invalid_response,
#: QUERY_LIMIT_EXCEEDED) — переходные: пробрасываем, item ретраится,
#: иначе id сообщения теряется и строка висит в фиде навсегда.
TOLERABLE_DELETE_CODES = frozenset({"MESSAGE_NOT_FOUND", "CANT_EDIT_MESSAGE"})


@dataclass(slots=True)
class AttachmentMeta:
    """Метаданные вложения сообщения (файл читает воркер из медиа-тома)."""

    file_path: str  # относительный путь в медиа-томе
    file_name: str | None = None
    mime_type: str | None = None
    size: int | None = None
    attachment_id: int | None = None  # для публичной подписанной ссылки


@dataclass(slots=True)
class NotifyDialogStats:
    """Предикат неотвеченности одного диалога (зеркало inbox-агрегата:
    неотвечен ⟺ есть inbound с id > max(id исходящих без is_autoreply))."""

    last_inbound_id: int | None
    last_inbound_text: str | None
    unanswered_count: int


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
    # Открытые линии B24 (imconnector): маршрутизация и сериализация.
    dialog_id: int | None = None  # chat.id коннектора (str(Dialog.id))
    chat_title: str | None = None  # Dialog.title → chat.name
    external_message_id: str | None = None  # message.id для send.messages
    message_created_at: datetime | None = None  # date unixsec входящего
    sent_at: datetime | None = None  # sent_at исходящего (delivery)
    contact_external_user_id: str | None = None  # user.id коннектора
    ol_line_id: str | None = None  # линия B24 аккаунта (None = классика)
    ol_active: bool = False
    b24_im_chat_id: int | None = None  # im-пара операторского исходящего
    b24_im_message_id: int | None = None
    # Автор исходящего (Manager.name по Message.author_user_id) — маркер
    # «↗️ Исходящее (ЧатМост, Иван)» в зеркале чата линии.
    author_name: str | None = None
    # Системный автоответ — в зеркале линии честно маркируется «Автоответ».
    is_autoreply: bool = False


class CrmSyncRepository:
    """Абстракция доступа к очереди crm_sync (+ данные сообщения)."""

    async def fetch_due(self, limit: int = 20) -> list[CrmSyncItem]: ...

    async def mark_done(self, item: CrmSyncItem) -> None: ...

    async def mark_failed(self, item: CrmSyncItem, error: str) -> None: ...

    async def reschedule(
        self,
        item: CrmSyncItem,
        *,
        delay_seconds: int,
        error: str | None = None,
        count_attempt: bool = True,
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

    async def get_ol_panel_mirror(self) -> bool:
        """Зеркалить ли панельные/БП-исходящие в чат линии (default: да —
        иначе разговор в OL-режиме разорван наполовину)."""
        return True

    async def get_crm_mode(self) -> str:
        """Какие карточки заводить новым клиентам (app_settings.crm_mode).

        Дефолт CRM_MODE_DEFAULT — фейки в тестах наследуют режим 'deal'."""
        return CRM_MODE_DEFAULT

    async def get_source_map(self) -> dict[Messenger, str]:
        """Маппинг канал→источник карточек (app_settings.source_map).

        Дефолт {} — фейки в тестах наследуют дефолты каналов."""
        return {}

    # --- Feed-уведомления (Wazzup-паритет) ---

    async def notify_dialog_stats(self, dialog_id: int) -> NotifyDialogStats:
        """Предикат неотвеченности диалога (см. NotifyDialogStats)."""

    async def notification_rows(self, dialog_id: int) -> list[DialogNotification]:
        """Слоты (диалог × адресат) с их b24_message_id."""

    async def upsert_notification_rows(
        self, dialog_id: int, recipient_ids: list[int]
    ) -> None:
        """Вставить слоты недостающих адресатов (UniqueConstraint — идемпо)."""

    async def remove_notification_row(self, row_id: int) -> None:
        """Удалить слот (адресат ушёл из линии)."""

    async def set_notification_message(
        self, row_id: int, b24_message_id: int | None
    ) -> None:
        """Записать id отправленного сообщения (None = погасить слот;
        оба варианта сбрасывают dismissed_at — см. pending_dismissed)."""

    async def pending_dismissed(self, limit: int = 20) -> list[DialogNotification]:
        """Слоты с висящим сообщением и стоявшим dismissed_at (sweep)."""

    async def has_newer_queued_notify(self, item_id: int, dialog_id: int) -> bool:
        """Есть ли более новый queued/retrying notify-item этого диалога
        (схлопывание бёрста входящих — REST-квота дороже лишнего рендера)."""


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
        openline: OpenLineService | None = None,
        im: ImService | None = None,
        token_mgr: TokenManager | None = None,
    ):
        self._repo = repo
        self._b24sync = b24sync
        self._max_attempts = max_attempts
        self._poll_interval = poll_interval
        self._batch_size = batch_size
        # Медиа-том (bridge): чтение файлов вложений для timeline-комментариев.
        self._media_storage = media_storage
        self._media_timeline_max_bytes = media_timeline_max_bytes
        # Открытые линии B24: None = сервис не сконфигурирован (тесты без OL).
        self._openline = openline
        # Feed-уведомления: None = ветка отключена (тесты без im-сервиса).
        self._im = im
        self._token_mgr = token_mgr
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
        source_map = await self._repo.get_source_map()
        items = await self._repo.fetch_due(self._batch_size)
        for item in items:
            if item.kind == KIND_INBOUND:
                await self._handle_inbound(
                    item, timeline_mode, media_to_timeline, crm_mode, source_map
                )
            elif item.kind == KIND_NOTIFY:
                await self._handle_notify(item)
            else:
                await self._handle_outbound(item, timeline_mode, media_to_timeline)
        # «Отвечать не нужно»: web ставит dismissed_at, сюда доезжает зачистка
        # висящих сообщений (состояние-как-очередь, без REST из web-процесса).
        try:
            await self._sweep_dismissed()
        except Exception:  # pragma: no cover - защитная сетка
            logger.exception("dismissed-sweep failed; continuing")

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
        self,
        item: CrmSyncItem,
        timeline_mode: str,
        media_to_timeline: bool,
        crm_mode: str,
        source_map: dict[Messenger, str],
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

        if data.ol_line_id:
            # Открытая линия: входящее уходит в нативный чат B24; наш
            # CRM-синк (карточки/таймлайн/уведомления) отключён — CRM
            # ведёт сама линия по своим правилам.
            await self._handle_ol_inbound(item, data)
            return

        try:
            result = await self._b24sync.process_inbound(
                sender_name=data.sender_name or "",
                sender_phone=data.sender_phone or "",
                message_text=data.message_text or "",
                assigned_b24_user_id=data.assigned_b24_user_id,
                messenger=data.messenger,
                existing_contact_id=data.crm_contact_id,
                existing_entity_id=data.crm_entity_id,
                existing_entity_type=data.crm_entity_type,
                crm_mode=crm_mode,
                source_map=source_map,
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
        # Feed-уведомление (Wazzup-паритет) — ОТДЕЛЬНЫМ item: падение im-вызовов
        # больше не ретраит CRM-синк (дефект дублей timeline-комментариев).
        # CRM-поля уже применены → ссылка на карточку в уведомлении свежая.
        try:
            await self._repo.enqueue(kind=KIND_NOTIFY, message_id=item.message_id)
        except Exception:
            logger.exception("notify enqueue failed for message_id=%s", item.message_id)

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

        if data.ol_line_id:
            await self._handle_ol_outbound(item, data)
            return

        try:
            # Feed-уведомления гасим ДО timeline-комментария: падение clear
            # ретраит item ещё до записи комментария (без дублей), а падение
            # комментария повторит clear как no-op (слоты уже NULL).
            if not data.is_autoreply:
                await self._clear_dialog_notifications(data.dialog_id or 0)
            # Классический таймлайн (не-OL): автоответ маркируем — иначе
            # текст бота выглядит в карточке ответом менеджера.
            text = data.message_text or ""
            if data.is_autoreply:
                text = f"[Автоответ] {text}"
            comment_id = await self._b24sync.process_outbound(
                dialog_entity_id=data.crm_entity_id,
                dialog_entity_type=data.crm_entity_type,
                contact_id=data.crm_contact_id,
                text=text,
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

    # ------------------------------------------------------------------ #
    # Feed-уведомления (Wazzup-паритет)
    # ------------------------------------------------------------------ #

    async def _handle_notify(self, item: CrmSyncItem) -> None:
        """Рендер строки диалога в чатах адресатов: на каждое входящее
        неотвеченного диалога — delete старой + add новой (чистый фид);
        ответ менеджера и «Отвечать не нужно» гасят (clear/sweep)."""
        data = await self._repo.collect(item.message_id)
        if data is None:
            logger.warning(
                "crm_sync item %s: message_id=%s not found — terminal fail",
                item.id,
                item.message_id,
            )
            await self._repo.mark_failed(item, "message_not_found")
            return
        if data.ol_line_id or self._im is None or self._token_mgr is None:
            # OL-диалоги живут в нативном чате линии (правило Wazzup); im-сервис
            # не сконфигурирован — уведомления выключены (тесты без im).
            await self._repo.mark_done(item)
            return
        dialog_id: int = data.dialog_id or 0
        try:
            if await self._repo.has_newer_queued_notify(item.id, dialog_id):
                # Бёрст входящих: более новый item отрендерит свежее состояние.
                await self._repo.mark_done(item)
                return
            stats = await self._repo.notify_dialog_stats(dialog_id)
            if stats.last_inbound_id is None or stats.unanswered_count == 0:
                # Ответ успел прийти до обработки — гасим висящие строки.
                await self._clear_dialog_notifications(dialog_id)
            else:
                await self._render_notification(data, dialog_id, stats)
        except Exception as exc:  # noqa: BLE001 — очередь обязана пережить любой сбой B24
            await self._fail_or_retry(item, exc)
            return
        await self._repo.mark_done(item)

    async def _render_notification(
        self, data: CrmSyncData, dialog_id: int, stats: NotifyDialogStats
    ) -> None:
        settings = get_settings()
        token = await self._token_mgr.get_token()
        if token is None:
            raise RuntimeError("no_b24_token")
        auth = token.access_token
        # Состав линии меняется — сводим слоты: ушедшим адресатам чистим
        # сообщения и слоты, новых вставляем (UniqueConstraint — идемпо).
        for row in await self._repo.notification_rows(dialog_id):
            if row.manager_b24_user_id in data.notify_user_ids:
                continue
            if row.b24_message_id is not None:
                await self._delete_im_quietly(auth, row.b24_message_id)
            await self._repo.remove_notification_row(row.id)
        await self._repo.upsert_notification_rows(dialog_id, data.notify_user_ids)

        profile = channel_profile(data.messenger)
        card_url = crm_card_url(
            settings.b24_portal,
            entity_id=data.crm_entity_id,
            entity_type=data.crm_entity_type,
            contact_id=data.crm_contact_id,
        )
        dismiss_button = None
        if settings.imbot_bot_id:
            # Чат-бот: COMMAND-кнопка — инлайн-гашение без вкладок.
            dismiss_button = dismiss_command_button(dialog_id)
        elif settings.public_base_url:
            dismiss_button = dismiss_link_button(
                sign_dismiss_url(
                    settings.public_base_url,
                    dialog_id,
                    secret=settings.session_secret,
                    ttl_sec=settings.notify_dismiss_ttl_sec,
                )
            )
        else:
            logger.warning("notify: ни бот, ни public_base_url — кнопки гашения не будет")
        text = build_notification_text(
            profile.notify_label,
            data.sender_name or data.sender_phone or "Без имени",
            stats.last_inbound_text,
            stats.unanswered_count,
        )
        keyboard = build_keyboard(card_url, dismiss_button)

        added: list[tuple[int, int]] = []
        for row in await self._repo.notification_rows(dialog_id):
            if row.b24_message_id is not None:
                await self._delete_im_quietly(auth, row.b24_message_id)
            new_id = await self._im.send_notification(
                auth, row.manager_b24_user_id, text, keyboard
            )
            await self._repo.set_notification_message(row.id, new_id)
            added.append((row.id, new_id))
        # ponytail: краш между send_notification и записью id оставляет
        # сироту-сообщение в чате адресата (ретрай добавит новое) — ручная
        # чистка; апгрейд (im.message.list-сверка при ретрае) при повторных
        # видимых дублях после крашей.
        # Пост-проверка гонки: менеджер ответил между предикатом и add —
        # гасим только что добавленные (clear-item тоже придёт, no-op).
        fresh = await self._repo.notify_dialog_stats(dialog_id)
        if fresh.unanswered_count == 0:
            for row_id, msg_id in added:
                await self._delete_im_quietly(auth, msg_id)
                await self._repo.set_notification_message(row_id, None)

    async def _clear_dialog_notifications(self, dialog_id: int) -> None:
        """Погасить строки диалога у всех адресатов: удалить сообщения в
        чатах, слоты обнулить. Идемпотентно (NULL-слоты = no-op).

        ponytail: перевод аккаунта с классики на открытую линию оставляет
        живые слоты (OL-ветки сюда не доходит) — гасятся кнопкой «Отвечать
        не нужно» (sweep OL-фильтра не имеет); чистка при привязке линии —
        при жалобах.
        """
        live = [
            row
            for row in await self._repo.notification_rows(dialog_id)
            if row.b24_message_id is not None
        ]
        if not live:
            return
        token = await self._token_mgr.get_token()
        if token is None:
            raise RuntimeError("no_b24_token")
        for row in live:
            await self._delete_im_quietly(token.access_token, row.b24_message_id)
            await self._repo.set_notification_message(row.id, None)

    async def _delete_im_quietly(self, auth: str, message_id: int) -> None:
        """Удаление сообщения фида. Перманентные B24-коды (TOLERANT_DELETE_
        CODES) — осознанная деградация (сообщение остаётся висеть, состояние
        нашей БД консистентно); переходные и сетевые — пробрасываются
        вызывающему (ретрай item, id не теряется)."""
        try:
            await self._im.delete_message(auth, message_id)
        except Bitrix24Error as exc:
            if exc.code not in TOLERABLE_DELETE_CODES:
                raise
            logger.warning(
                "notify: delete_message %s degraded (%s) — строка останется в чате",
                message_id,
                exc.code,
            )

    async def _sweep_dismissed(self) -> None:
        """«Отвечать не нужно»: web ставит dismissed_at, сюда доезжают слоты
        с висящим сообщением. Токена нет — тихо пропускаем (следующее
        входящее/ответ всё равно сведёт состояние)."""
        if self._im is None or self._token_mgr is None:
            return
        rows = await self._repo.pending_dismissed(20)
        if not rows:
            return
        token = await self._token_mgr.get_token()
        if token is None:
            return
        for row in rows:
            await self._delete_im_quietly(token.access_token, row.b24_message_id)
            await self._repo.set_notification_message(row.id, None)

    async def _handle_ol_inbound(self, item: CrmSyncItem, data: CrmSyncData) -> None:
        """Входящее → imconnector.send.messages (чат открытой линии)."""
        if not data.ol_active:
            # Коннектор деактивирован: ждём реактивации, попытки не сжигаем
            # (паттерн outbox no_provider). LINEDELETE/STATUSDELETE почистят
            # привязку — очередь вернётся к классическому CRM-синку.
            await self._repo.reschedule(
                item, delay_seconds=300, error="ol_inactive", count_attempt=False
            )
            return
        message = build_send_message(
            message_id=data.external_message_id or str(item.message_id),
            dialog_id=data.dialog_id or 0,
            date_unixsec=to_unixsec(data.message_created_at or datetime.now(UTC)),
            text=data.message_text,
            user_id=f"{data.messenger.value}_{data.contact_external_user_id or ''}",
            user_name=data.sender_name,
            user_last_name=data.sender_last_name,
            files=self._ol_files(data),
            chat_name=data.chat_title,
        )
        try:
            if self._openline is None:  # pragma: no cover - wiring всегда передаёт
                raise RuntimeError("no_openline_service")
            sent = await self._openline.send_messages(
                line_id=data.ol_line_id, messages=[message]
            )
            if sent is None:
                # Нет B24-токена: ретраибельно (с burn-попытками), как в CRM-ветке.
                raise RuntimeError("no_b24_token")
        except Bitrix24Error as exc:
            if exc.code == "NOT_ACTIVE_LINE":
                # Линию выключили на портале: ждём, попытки не сжигаем.
                await self._repo.reschedule(
                    item, delay_seconds=300, error=exc.code, count_attempt=False
                )
                return
            await self._fail_or_retry(item, exc)
            return
        except Exception as exc:  # noqa: BLE001 — очередь обязана переживать любой сбой
            await self._fail_or_retry(item, exc)
            return
        await self._repo.mark_done(item)

    async def _handle_ol_outbound(self, item: CrmSyncItem, data: CrmSyncData) -> None:
        """Исходящее линии: операторскому (im-пара) — статус доставки,
        панельному/БП/инициации (без пары) — зеркало в чат линии."""
        if not (data.b24_im_chat_id and data.b24_im_message_id):
            if not data.ol_active:
                # Коннектор деактивирован: ждём реактивации/перепривязки —
                # очередь вернётся к классическому CRM-синку (как inbound).
                await self._repo.reschedule(
                    item, delay_seconds=300, error="ol_inactive", count_attempt=False
                )
                return
            if not await self._repo.get_ol_panel_mirror():
                await self._repo.mark_done(item)
                return
            # imconnector инжектит в чат линии только «от клиента»: user =
            # клиент (тред сохраняется, ответ клиента придёт сюда же), а
            # автор-менеджер честно виден в префиксе текста.
            author = f", {data.author_name}" if data.author_name else ""
            kind = "Автоответ" if data.is_autoreply else "Исходящее"
            message = build_send_message(
                message_id=data.external_message_id or str(item.message_id),
                dialog_id=data.dialog_id or 0,
                date_unixsec=to_unixsec(
                    data.sent_at or data.message_created_at or datetime.now(UTC)
                ),
                text=f"↗️ {kind} (ЧатМост{author}): {data.message_text or ''}",
                user_id=f"{data.messenger.value}_{data.contact_external_user_id or ''}",
                user_name=data.sender_name,
                files=self._ol_files(data),
                chat_name=data.chat_title,
            )
            if await self._ol_send_outbound(
                item, line_id=data.ol_line_id, messages=[message], delivery=False
            ):
                await self._repo.mark_done(item)
            return
        payload = build_delivery_message(
            dialog_id=data.dialog_id or 0,
            im_chat_id=data.b24_im_chat_id,
            im_message_id=data.b24_im_message_id,
            date_unixsec=to_unixsec(
                data.sent_at or data.message_created_at or datetime.now(UTC)
            ),
        )
        if await self._ol_send_outbound(
            item, line_id=data.ol_line_id, messages=[payload], delivery=True
        ):
            await self._repo.mark_done(item)

    async def _ol_send_outbound(
        self,
        item: CrmSyncItem,
        *,
        line_id: str | None,
        messages: list[dict],
        delivery: bool,
    ) -> bool:
        """Общий хвост OL-исходящих. True = отправлено (вызывающий делает
        mark_done); False — обработано здесь: NOT_ACTIVE_LINE — done с
        потерей косметики (статус/зеркало, сообщение уже доставлено),
        прочее — fail_or_retry."""
        try:
            if self._openline is None:  # pragma: no cover - wiring всегда передаёт
                raise RuntimeError("no_openline_service")
            if delivery:
                sent = await self._openline.send_status_delivery(
                    line_id=line_id, messages=messages
                )
            else:
                sent = await self._openline.send_messages(
                    line_id=line_id, messages=messages
                )
            if sent is None:
                # Нет B24-токена: ретраибельно (с burn-попытками), как в CRM-ветке.
                raise RuntimeError("no_b24_token")
        except Bitrix24Error as exc:
            if exc.code == "NOT_ACTIVE_LINE":
                logger.warning(
                    "crm_sync item %s: OL delivery/mirror lost: %s", item.id, exc.code
                )
                await self._repo.mark_done(item)
                return False
            await self._fail_or_retry(item, exc)
            return False
        except Exception as exc:  # noqa: BLE001 — очередь обязана переживать любой сбой B24
            await self._fail_or_retry(item, exc)
            return False
        return True

    def _ol_files(self, data: CrmSyncData) -> list[dict[str, str]]:
        """Вложения → files[] для send.messages (публичные подписанные URL)."""
        settings = get_settings()
        if not data.attachments:
            return []
        if not settings.public_base_url:
            logger.warning("OL: public_base_url пуст — вложения уйдут без файла")
            return []
        files: list[dict[str, str]] = []
        for att in data.attachments:
            if att.attachment_id is None:
                continue
            files.append(
                {
                    "url": sign_media_url(
                        settings.public_base_url,
                        att.attachment_id,
                        secret=settings.session_secret,
                        ttl_sec=settings.media_public_ttl_sec,
                    ),
                    "name": att.file_name or Path(att.file_path).name,
                }
            )
        return files

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
