"""Автоответы на входящие: «первое входящее» и «нерабочее время».

Семантика Wazzup: автоответ НЕ снимает счётчик «Ожидают ответа» — агрегат
web/routes/inbox.py игнорирует исходящие с ``Message.is_autoreply``.

Матрица выбора (pick_reply):
  - вне рабочих часов И триггер offhours включён → текст offhours (не чаще
    1 раза в 24 ч на диалог; не влезает в живой разговор — после предыдущего
    входящего был реальный ответ менеджера);
  - иначе «первое входящее» → приветствие самому первому inbound диалога
    (включая ночью, если offhours выключен — конкуренции триггеров нет).
    Первый ответ клиента на нашу инициацию тоже считается первым входящим.

Fail-closed: пустой текст = триггер выключен; мусор в work_hours/таймзоне =
дефолт (Пн–Пт 09:00–18:00 Europe/Moscow); исключение автоответа глушится
логом ВЫШЕ по стеку (incoming_handler) — входящее уже закоммичено.
"""

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AppSetting, Dialog, Message, MessageDirection, MessageStatus

logger = logging.getLogger(__name__)

AUTO_REPLY_FIRST_ENABLED_KEY = "auto_reply_first_enabled"  # "on" | "off"
AUTO_REPLY_FIRST_TEXT_KEY = "auto_reply_first_text"
AUTO_REPLY_OFFHOURS_ENABLED_KEY = "auto_reply_offhours_enabled"  # "on" | "off"
AUTO_REPLY_OFFHOURS_TEXT_KEY = "auto_reply_offhours_text"
WORK_HOURS_KEY = "work_hours"  # JSON {"days":[0..6],"start":"09:00","end":"18:00"}
WORK_HOURS_TZ_KEY = "work_hours_tz"  # IANA, default Europe/Moscow

AUTOREPLY_KEYS = (
    AUTO_REPLY_FIRST_ENABLED_KEY,
    AUTO_REPLY_FIRST_TEXT_KEY,
    AUTO_REPLY_OFFHOURS_ENABLED_KEY,
    AUTO_REPLY_OFFHOURS_TEXT_KEY,
    WORK_HOURS_KEY,
    WORK_HOURS_TZ_KEY,
)

#: Ночные интервалы через полночь (start > end) не поддерживаем — честный
#: отказ в PUT настроек; здесь это последний рубеж (мусор в БД → дефолт).
_DEFAULT_WORK_HOURS = {"days": [0, 1, 2, 3, 4], "start": "09:00", "end": "18:00"}
DEFAULT_WORK_TZ = "Europe/Moscow"

#: Дедуп offhours-автоответа: не чаще одного на диалог за окно.
OFFHOURS_DEDUP_SEC = 24 * 3600


@dataclass(slots=True)
class AutoReplyConfig:
    first_enabled: bool = False
    first_text: str | None = None
    offhours_enabled: bool = False
    offhours_text: str | None = None
    days: frozenset[int] = frozenset(_DEFAULT_WORK_HOURS["days"])  # Пн=0
    start_min: int = 9 * 60
    end_min: int = 18 * 60
    tz: str = DEFAULT_WORK_TZ


def _parse_hhmm(value: str) -> int | None:
    """"09:00" → 540; мусор → None."""
    parts = value.split(":")
    if len(parts) != 2 or not (parts[0].isdigit() and parts[1].isdigit()):
        return None
    hours, minutes = int(parts[0]), int(parts[1])
    if hours > 23 or minutes > 59:
        return None
    return hours * 60 + minutes


def parse_work_hours(raw: str | None) -> tuple[frozenset[int], int, int]:
    """(days, start_min, end_min) из JSON app_settings; мусор → дефолт."""
    try:
        data = json.loads(raw) if raw else None
        days = frozenset(d for d in data["days"] if isinstance(d, int) and 0 <= d <= 6)
        start = _parse_hhmm(data["start"])
        end = _parse_hhmm(data["end"])
        if days and start is not None and end is not None and start < end:
            return days, start, end
    except (TypeError, ValueError, KeyError):
        pass
    return (
        frozenset(_DEFAULT_WORK_HOURS["days"]),
        9 * 60,
        18 * 60,
    )


def _valid_tz(name: str | None) -> str:
    try:
        ZoneInfo(name or "")
    except Exception:  # noqa: BLE001 — мусор из app_settings: любой разбор = дефолт
        return DEFAULT_WORK_TZ
    return name  # type: ignore[return-value]


async def read_auto_reply_config(session: AsyncSession) -> AutoReplyConfig:
    """Конфиг одним batch-запросом (горячий путь: каждое входящее)."""
    rows = (
        await session.execute(select(AppSetting).where(AppSetting.key.in_(AUTOREPLY_KEYS)))
    ).scalars().all()
    kv = {r.key: r.value for r in rows}
    # Enabled-ключ требует непустой текст: пустой текст = выключен (fail-closed).
    first_text = (kv.get(AUTO_REPLY_FIRST_TEXT_KEY) or "").strip() or None
    offhours_text = (kv.get(AUTO_REPLY_OFFHOURS_TEXT_KEY) or "").strip() or None
    days, start_min, end_min = parse_work_hours(kv.get(WORK_HOURS_KEY))
    return AutoReplyConfig(
        first_enabled=kv.get(AUTO_REPLY_FIRST_ENABLED_KEY) == "on" and first_text is not None,
        first_text=first_text,
        offhours_enabled=(
            kv.get(AUTO_REPLY_OFFHOURS_ENABLED_KEY) == "on" and offhours_text is not None
        ),
        offhours_text=offhours_text,
        days=days,
        start_min=start_min,
        end_min=end_min,
        tz=_valid_tz(kv.get(WORK_HOURS_TZ_KEY)),
    )


async def get_auto_reply_config(session_factory) -> AutoReplyConfig:
    """Обёртка для web-процесса (паттерн get_ol_panel_mirror)."""
    async with session_factory() as s:
        return await read_auto_reply_config(s)


async def save_auto_reply_fields(session_factory, fields: dict[str, str]) -> None:
    """Атомарный batch-upsert переданных ключей (одна txn).

    Одна транзакция на весь PUT — крэш на середине не оставляет
    «включили, но текст не доехал».
    """
    async with session_factory() as s:
        rows = (
            await s.execute(select(AppSetting).where(AppSetting.key.in_(fields)))
        ).scalars().all()
        existing = {r.key: r for r in rows}
        for key, value in fields.items():
            if key in existing:
                existing[key].value = value
            else:
                s.add(AppSetting(key=key, value=value))
        await s.commit()


def in_work_hours(cfg: AutoReplyConfig, ts: datetime) -> bool:
    """ts (aware UTC; naive трактуем как UTC) внутри расписания в cfg.tz."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    local = ts.astimezone(ZoneInfo(cfg.tz))
    if local.weekday() not in cfg.days:
        return False
    minutes = local.hour * 60 + local.minute
    return cfg.start_min <= minutes < cfg.end_min


def pick_reply(
    cfg: AutoReplyConfig,
    *,
    is_first_inbound: bool,
    in_hours: bool,
    last_autoreply_age_sec: float | None,
    manager_answered: bool,
) -> str | None:
    """Чистая матрица выбора текста (юнит-тестируется без БД). None = молчим."""
    if not in_hours and cfg.offhours_enabled:
        if manager_answered:
            return None
        if last_autoreply_age_sec is not None and last_autoreply_age_sec < OFFHOURS_DEDUP_SEC:
            return None
        return cfg.offhours_text
    if cfg.first_enabled and is_first_inbound:
        return cfg.first_text
    return None


class AutoReplier:
    """Хук после входящего: решить и поставить автоответ в outbox.

    Отдельная сессия/txn ПОСЛЕ коммита входящего — сбой здесь не теряет
    входящее (вызывающий глушит исключения логом). Message+OutboxItem
    атомарны в одной txn (enqueue не коммитит сам).
    """

    def __init__(self, session_factory):
        self._session_factory = session_factory

    async def on_inbound(
        self, *, message_id: int, account_id: int, msg_timestamp: datetime | None
    ) -> None:
        from app.bridge.outbox_repo_sqlalchemy import SqlAlchemyOutboxRepository

        async with self._session_factory() as session:
            cfg = await read_auto_reply_config(session)
            if not cfg.first_enabled and not cfg.offhours_enabled:
                return
            dialog_id = (
                await session.execute(
                    select(Message.dialog_id).where(Message.id == message_id)
                )
            ).scalar_one_or_none()
            if dialog_id is None:
                return

            # Три скаляра по индексу dialog_id: «первое ли входящее» и дедуп
            # offhours (последний is_autoreply), guard «после предыдущего
            # inbound был реальный ответ менеджера».
            last_autoreply_at = await session.scalar(
                select(Message.created_at)
                .where(
                    Message.dialog_id == dialog_id,
                    Message.direction == MessageDirection.outbound,
                    Message.is_autoreply.is_(True),
                )
                .order_by(Message.id.desc())
                .limit(1)
            )
            prev_inbound_id = await session.scalar(
                select(Message.id)
                .where(
                    Message.dialog_id == dialog_id,
                    Message.direction == MessageDirection.inbound,
                    Message.id < message_id,
                )
                .order_by(Message.id.desc())
                .limit(1)
            )
            last_real_out_id = await session.scalar(
                select(Message.id)
                .where(
                    Message.dialog_id == dialog_id,
                    Message.direction == MessageDirection.outbound,
                    Message.is_autoreply.is_(False),
                )
                .order_by(Message.id.desc())
                .limit(1)
            )
            manager_answered = (
                prev_inbound_id is not None
                and last_real_out_id is not None
                and last_real_out_id > prev_inbound_id
            )
            now = datetime.now(UTC)
            inbound_at = msg_timestamp or now
            in_hours = in_work_hours(cfg, inbound_at)
            last_age = None
            if last_autoreply_at is not None:
                stale = last_autoreply_at
                if stale.tzinfo is None:  # SQLite-тесты хранят naive
                    stale = stale.replace(tzinfo=UTC)
                last_age = (inbound_at - stale).total_seconds()
            text = pick_reply(
                cfg,
                is_first_inbound=prev_inbound_id is None,
                in_hours=in_hours,
                last_autoreply_age_sec=last_age,
                manager_answered=manager_answered,
            )
            if text is None:
                return

            dialog = await session.get(Dialog, dialog_id)
            message = Message(
                dialog_id=dialog_id,
                direction=MessageDirection.outbound,
                text=text,
                status=MessageStatus.pending,
                author_user_id=None,
                is_autoreply=True,
            )
            session.add(message)
            await session.flush()
            dialog.last_msg_at = message.created_at
            repo = SqlAlchemyOutboxRepository(session)
            await repo.enqueue(
                dialog_id=dialog_id,
                tg_account_id=account_id,
                external_chat_id=dialog.external_chat_id,
                text=text,
                is_initiation=False,
                message_id=message.id,
            )
            await session.commit()
            logger.info(
                "Автоответ поставлен в очередь: dialog_id=%s account_id=%s "
                "(in_hours=%s)",
                dialog_id,
                account_id,
                in_hours,
            )
