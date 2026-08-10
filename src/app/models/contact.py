"""Контакт — клиент/собеседник в мессенджере, связанный с Bitrix24 CRM."""

from sqlalchemy import BigInteger, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class Contact(Base, TimestampMixin):
    __tablename__ = "contacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tg_user_id: Mapped[int] = mapped_column(
        BigInteger, unique=True, index=True, nullable=False
    )
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    crm_contact_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True, index=True
    )

    dialogs: Mapped[list["Dialog"]] = relationship(back_populates="contact")
