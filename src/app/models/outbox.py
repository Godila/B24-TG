"""Outbox — очередь исходящих сообщений для надёжной доставки.

Таблица сканируется OutboxWorker'ом (Task 11). ``is_initiation`` отличает
инициирующие сообщения (требуют FloodWait-защиты и привязки диалога) от
ответов; ``external_chat_id`` хранит уже известный chat_id, чтобы воркеру
не приходилось его повторно вычислять.
"""

import enum
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.attachment import Attachment


class OutboxStatus(str, enum.Enum):
    queued = "queued"
    sending = "sending"
    sent = "sent"
    failed = "failed"
    retrying = "retrying"


class OutboxItem(Base, TimestampMixin):
    __tablename__ = "outbox"

    # BigInteger на Postgres; на SQLite автоинкремент работает только для
    # INTEGER PRIMARY KEY, поэтому через variant используем Integer.
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    dialog_id: Mapped[int] = mapped_column(
        ForeignKey("dialogs.id"), nullable=False, index=True
    )
    # Ссылка на Message(direction=outbound): по ней воркер после отправки
    # закрывает исходящий цикл (pending -> sent/error, external_message_id, sent_at).
    # nullable — исторические строки и внутренние постановки в очередь без Message.
    message_id: Mapped[int | None] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("messages.id"),
        nullable=True,
        index=True,
    )
    tg_account_id: Mapped[int] = mapped_column(
        ForeignKey("tg_accounts.id"), nullable=False, index=True
    )
    external_chat_id: Mapped[str] = mapped_column(String(128), nullable=False)
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    attachment_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("attachments.id"), nullable=True
    )
    # Вложение исходящего медиа. Читается только selectinload-ом в
    # fetch_due: lazy-доступ в async-контексте роняет MissingGreenlet.
    attachment: Mapped["Attachment | None"] = relationship()
    is_initiation: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[OutboxStatus] = mapped_column(
        Enum(OutboxStatus), default=OutboxStatus.queued, index=True
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    last_error: Mapped[str | None] = mapped_column(String(512), nullable=True)
