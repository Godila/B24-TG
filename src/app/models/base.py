"""Декларативный базовый класс и миксины для всех ORM-моделей."""

from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Единый ``DeclarativeBase`` для всех таблиц приложения.

    ``Base.metadata`` собирает все таблицы, описанные в подклассах; ``alembic``
    и ``create_all`` в тестах используют именно его.
    """



class TimestampMixin:
    """Добавляет ``created_at`` / ``updated_at`` со server-side дефолтами."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
