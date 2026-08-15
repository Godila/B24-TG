"""Отдельное сообщение в диалоге (входящее или исходящее)."""

import enum
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.attachment import Attachment
    from app.models.dialog import Dialog


class MessageDirection(str, enum.Enum):
    inbound = "in"
    outbound = "out"


class MessageStatus(str, enum.Enum):
    pending = "pending"
    sent = "sent"
    delivered = "delivered"
    read = "read"
    error = "error"


class Message(Base, TimestampMixin):
    __tablename__ = "messages"

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
    direction: Mapped[MessageDirection] = mapped_column(
        Enum(
            MessageDirection,
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
    )
    # Внешний id сообщения в канале. TG: числовой id MTProto; MAX: числовой
    # id web-протокола (приходит числом, хранится строкой — id MAX длинные,
    # а Bot API отдаёт строковые mid). Дедуп входящих идёт по
    # (dialog_id, external_message_id).
    external_message_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[MessageStatus] = mapped_column(
        Enum(MessageStatus), default=MessageStatus.pending
    )
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    author_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    timeline_comment_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    dialog: Mapped["Dialog"] = relationship(back_populates="messages")
    attachments: Mapped[list["Attachment"]] = relationship(back_populates="message")
