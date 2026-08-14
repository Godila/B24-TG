"""Диалог между контактом и (опционально) назначенным менеджером в мессенджере."""

import enum
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.contact import Contact
    from app.models.message import Message


class Messenger(str, enum.Enum):
    tg = "tg"
    max = "max"


class DialogStatus(str, enum.Enum):
    active = "active"
    archived = "archived"


class Dialog(Base, TimestampMixin):
    __tablename__ = "dialogs"
    # Мультиаккаунт: в приватных TG-чатах external_chat_id == tg-id клиента
    # и совпадает у всех менеджеров, поэтому уникальна пара
    # (external_chat_id, assigned_user_id), а не chat_id сам по себе.
    # Констрейнт создаёт и составной индекс — отдельный не нужен.
    __table_args__ = (
        UniqueConstraint(
            "external_chat_id",
            "assigned_user_id",
            name="uq_dialogs_chat_per_manager",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    contact_id: Mapped[int] = mapped_column(
        ForeignKey("contacts.id"), nullable=False, index=True
    )
    messenger: Mapped[Messenger] = mapped_column(Enum(Messenger), nullable=False)
    external_chat_id: Mapped[str] = mapped_column(String(128), nullable=False)
    crm_deal_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    crm_entity_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    assigned_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[DialogStatus] = mapped_column(
        Enum(DialogStatus), default=DialogStatus.active
    )
    last_msg_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    contact: Mapped["Contact"] = relationship(back_populates="dialogs")
    messages: Mapped[list["Message"]] = relationship(back_populates="dialog")
