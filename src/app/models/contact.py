"""Контакт — клиент/собеседник в мессенджере, связанный с Bitrix24 CRM.

Идентичность канальная: пара (messenger, external_user_id). Один и тот же
человек в TG и MAX — две строки (у них разные внешние id); склейка человека
происходит на стороне CRM по телефону (общий crm_contact_id).
"""

from typing import TYPE_CHECKING

from sqlalchemy import Enum, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin
from app.models.dialog import Messenger

if TYPE_CHECKING:
    from app.models.dialog import Dialog


class Contact(Base, TimestampMixin):
    __tablename__ = "contacts"
    __table_args__ = (
        UniqueConstraint(
            "messenger", "external_user_id", name="uq_contacts_messenger_external_user_id"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    messenger: Mapped[Messenger] = mapped_column(
        Enum(Messenger), default=Messenger.tg, nullable=False
    )
    external_user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Раздельные имя/фамилия от канала (для CRM NAME/LAST_NAME); name —
    # полное отображаемое имя (виджет, уведомления).
    first_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    crm_contact_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)

    dialogs: Mapped[list["Dialog"]] = relationship(back_populates="contact")
