"""Отдельное сообщение в диалоге (входящее или исходящее)."""

import enum
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    select,
    text,
)
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


# Дедуп событий ONIMCONNECTORMESSAGEADD на уровне БД: дубль доставки события
# = дубль отправки клиенту; SELECT-проверка одна гонку не закрывает.
_B24_IM_UNIQUE_WHERE = text("b24_im_message_id IS NOT NULL")


class Message(Base, TimestampMixin):
    __tablename__ = "messages"
    __table_args__ = (
        Index(
            "uq_messages_dialog_b24_im_message",
            "dialog_id",
            "b24_im_message_id",
            unique=True,
            sqlite_where=_B24_IM_UNIQUE_WHERE,
            postgresql_where=_B24_IM_UNIQUE_WHERE,
        ),
    )

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
    # Открытые линии B24: im-пара события ONIMCONNECTORMESSAGEADD (id чата и
    # сообщения B24) — нужна для imconnector.send.status.delivery после
    # реальной отправки в мессенджер. Только у исходящих операторов из чата
    # линии. Прецедент точечной B24-колонки — timeline_comment_id.
    b24_im_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    b24_im_message_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, index=True
    )

    dialog: Mapped["Dialog"] = relationship(back_populates="messages")
    attachments: Mapped[list["Attachment"]] = relationship(back_populates="message")


async def has_inbound(session, dialog_id: int) -> bool:
    """Есть ли в диалоге хоть одно входящее (→ is_initiation отправки).

    Единственная каноническая реализация предиката анти-бан throttler:
    «диалог без ответа клиента». Паттерн модульной функции — как
    terminate_active_commands (login_command.py). ``session`` — любая
    AsyncSession (роут/воркер/тест).
    """
    row = await session.execute(
        select(Message.id)
        .where(
            Message.dialog_id == dialog_id,
            Message.direction == MessageDirection.inbound,
        )
        .limit(1)
    )
    return row.scalar_one_or_none() is not None
