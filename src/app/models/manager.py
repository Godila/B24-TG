"""Модель менеджера (пользователь Bitrix24, ведущий диалоги)."""

import enum

from sqlalchemy import Boolean, Enum, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class ManagerRole(str, enum.Enum):
    manager = "manager"
    supervisor = "supervisor"


class Manager(Base, TimestampMixin):
    __tablename__ = "managers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    b24_user_id: Mapped[int] = mapped_column(
        Integer, unique=True, nullable=False, index=True
    )
    role: Mapped[ManagerRole] = mapped_column(
        Enum(ManagerRole), default=ManagerRole.manager
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    tg_account: Mapped["TgAccount | None"] = relationship(
        back_populates="manager", uselist=False
    )
