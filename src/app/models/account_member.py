"""Участник линии: аккаунт мессенджера ↔ менеджер с ролью.

Линия — сам аккаунт (``tg_accounts``): «личный номер» — аккаунт с одним
участником, «общий» — с несколькими. Роль: ``participant`` пишет из линии,
``observer`` только читает. Надзор и админка — глобальная роль
``Manager.role`` и к составу линии отношения не имеют.
"""

import enum

from sqlalchemy import Enum, ForeignKey, Integer, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class LineRole(str, enum.Enum):
    participant = "participant"
    observer = "observer"


class AccountMember(Base, TimestampMixin):
    __tablename__ = "account_members"
    __table_args__ = (
        # Один менеджер в линии — с одной ролью.
        UniqueConstraint("account_id", "manager_id", name="uq_account_members_pair"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tg_accounts.id"), nullable=False, index=True
    )
    manager_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("managers.id"), nullable=False, index=True
    )
    role: Mapped[LineRole] = mapped_column(
        Enum(LineRole), default=LineRole.participant, nullable=False
    )
