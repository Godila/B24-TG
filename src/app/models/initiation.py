"""Initiation — команда «написать первым» (web → bridge).

Одна строка = конечный автомат одного инициирующего сообщения: web пишет
pending-строку и поллит статус, InitiationWorker (bridge) резолвит peer
живым провайдером и в одной транзакции создаёт Contact/Dialog/Message/
OutboxItem. Паттерн — login_commands (провайдеры живут только в bridge).
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
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin
from app.models.dialog import Messenger


class InitiationStatus(str, enum.Enum):
    pending = "pending"  # вставлена web'ом, bridge не резолвил
    linked = "linked"    # терминальный успех: диалог создан, сообщение в outbox
    failed = "failed"    # терминальный отказ (не найден / аккаунт офлайн / сбой)


class Initiation(Base, TimestampMixin):
    __tablename__ = "initiations"
    __table_args__ = (
        # Не более одной живой инициализации на (аккаунт, dest) — дубль
        # двойного клика ловит БД (роут маппит IntegrityError → 409).
        Index(
            "uq_initiations_active",
            "account_id",
            "dest_value",
            unique=True,
            postgresql_where=text("status = 'pending'"),
            sqlite_where=text("status = 'pending'"),
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    account_id: Mapped[int] = mapped_column(
        ForeignKey("tg_accounts.id"), nullable=False, index=True
    )
    messenger: Mapped[Messenger] = mapped_column(Enum(Messenger), nullable=False)
    author_manager_id: Mapped[int] = mapped_column(
        ForeignKey("managers.id"), nullable=False, index=True
    )
    #: b24-uid автора → Message.author_user_id (заполняем заранее: строка
    #: самодостаточна, воркеру не нужен join Manager).
    author_b24_user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    #: Карточка, из которой инициировали: 'deal' | 'lead' | 'contact'.
    entity_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dest_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    dest_value: Mapped[str] = mapped_column(String(128), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[InitiationStatus] = mapped_column(
        Enum(InitiationStatus), default=InitiationStatus.pending, nullable=False
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    last_error: Mapped[str | None] = mapped_column(String(512), nullable=True)
    dialog_id: Mapped[int | None] = mapped_column(
        ForeignKey("dialogs.id"), nullable=True
    )
