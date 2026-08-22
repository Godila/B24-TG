"""CrmSync — очередь CRM-синхронизации сообщений (план 006).

Входящее/исходящее сообщение сначала сохраняется в нашей БД (Inbound /
Outbox), а CRM-записи (контакт/сделка/timeline-комментарий/уведомление)
делает CrmSyncWorker асинхронно отсюда — с ретраями и backoff, как outbox.
``kind``: 'inbound' — синк входящего (process_inbound), 'outbound' —
timeline-комментарий исходящего (process_outbound), 'notify' — feed-
уведомление менеджерам (dialog_notifications).
"""

import enum
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

# kind-значения очереди (String-колонка, не Enum — по образцу external_chat_id).
KIND_INBOUND = "inbound"
KIND_OUTBOUND = "outbound"
# Feed-уведомление менеджерам (Wazzup-паритет): рендер строки диалога в
# чатах адресатов (delete старой + add новой), постановка — из воркера
# после классического inbound-синка.
KIND_NOTIFY = "notify"


class CrmSyncStatus(str, enum.Enum):
    queued = "queued"
    done = "done"
    failed = "failed"
    # reschedule() переводит элемент в retrying; fetch_due берёт и queued,
    # и retrying — иначе отложенные задачи зависнут навсегда (как в outbox).
    retrying = "retrying"


class CrmSyncItem(Base, TimestampMixin):
    __tablename__ = "crm_sync"
    __table_args__ = (
        # Покрывает выборку fetch_due: status IN (...) AND next_attempt_at <= now.
        Index("ix_crm_sync_status_next_attempt_at", "status", "next_attempt_at"),
    )

    # BigInteger на Postgres; на SQLite автоинкремент работает только для
    # INTEGER PRIMARY KEY, поэтому через variant используем Integer.
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    message_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("messages.id"),
        nullable=False,
        index=True,
    )
    status: Mapped[CrmSyncStatus] = mapped_column(
        Enum(CrmSyncStatus), default=CrmSyncStatus.queued
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_error: Mapped[str | None] = mapped_column(String(512), nullable=True)
