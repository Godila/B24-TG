"""Вложение к сообщению (фото/файл/видео/голос/стикер)."""

import enum

from sqlalchemy import BigInteger, Enum, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class AttachmentType(str, enum.Enum):
    photo = "photo"
    file = "file"
    video = "video"
    voice = "voice"
    sticker = "sticker"


class Attachment(Base, TimestampMixin):
    __tablename__ = "attachments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("messages.id"), nullable=False
    )
    type: Mapped[AttachmentType] = mapped_column(Enum(AttachmentType), nullable=False)
    file_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    message: Mapped["Message"] = relationship(back_populates="attachments")
