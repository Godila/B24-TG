"""Telegram-аккаунт, привязанный к менеджеру 1:1."""

import enum
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.manager import Manager


class TgAccountStatus(str, enum.Enum):
    active = "active"
    banned = "banned"
    offline = "offline"


class TgAccount(Base, TimestampMixin):
    __tablename__ = "tg_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    phone: Mapped[str] = mapped_column(
        String(32), unique=True, nullable=False, index=True
    )
    session_path: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[TgAccountStatus] = mapped_column(
        Enum(TgAccountStatus), default=TgAccountStatus.offline
    )
    manager_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("managers.id"), unique=True, nullable=False
    )
    last_floodwait_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    manager: Mapped["Manager"] = relationship(back_populates="tg_account")
