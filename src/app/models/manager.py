"""Модель менеджера (пользователь Bitrix24, ведущий диалоги)."""

import enum

from sqlalchemy import JSON, Boolean, Enum, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

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
    #: Режим «только чтение»: POST сообщений → 403, виджет прячет composer.
    #: Политика на НОВЫЕ отправки; уже поставленные в outbox отправляются
    #: (жёсткий транспортный стоп — деактивация аккаунта).
    is_readonly: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: Приоритетный аккаунт исходящих «написать первым» по каналам:
    #: {"tg": <tg_accounts.id>, "max": <id>}. Нет ключа → единственный
    #: доступный аккаунт канала. Валидация id — в момент использования.
    default_outbound: Mapped[dict | None] = mapped_column(JSON, nullable=True)
