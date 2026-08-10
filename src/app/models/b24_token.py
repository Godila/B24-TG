"""OAuth-токены Bitrix24: хранилище access/refresh токенов портала."""

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class B24Token(Base, TimestampMixin):
    """OAuth-токены Bitrix24. Один портал = одна запись (member_id unique)."""

    __tablename__ = "b24_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    member_id: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True
    )
    access_token: Mapped[str] = mapped_column(Text, nullable=False)
    refresh_token: Mapped[str] = mapped_column(Text, nullable=False)
    client_endpoint: Mapped[str] = mapped_column(String(255), nullable=False)
    portal: Mapped[str] = mapped_column(String(255), nullable=False)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    scope: Mapped[str] = mapped_column(Text, nullable=False, default="")
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
