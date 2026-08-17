"""Вложение к сообщению (фото/файл/видео/голос/стикер)."""

import enum
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, Enum, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.message import Message


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
    # Путь ВНУТРИ медиа-тома (POSIX-относительный: "in/<uuid>.jpg") —
    # резолвит MediaStorage; абсолютные пути в БД ломаются при смене тома.
    file_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Оригинальное имя файла (для отображения и Content-Disposition).
    # У TG-photo имени нет — NULL.
    file_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    message: Mapped["Message"] = relationship(back_populates="attachments")
