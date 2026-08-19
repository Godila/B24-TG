"""Канальный аккаунт, привязанный к менеджеру (по одному на канал).

Таблица исторически называется ``tg_accounts`` (канал Telegram был первым);
с добавлением MAX в ней живут аккаунты обоих каналов — колонка ``messenger``
различает их, а уникальности составные: (messenger, manager_id) и
(messenger, phone). TG-строки используют ``session_path`` (файл Telethon),
MAX-строки — ``token``/``device_id`` (сессия web-клиента MAX).
"""

import enum
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, Enum, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin
from app.models.dialog import Messenger

if TYPE_CHECKING:
    from app.models.account_member import AccountMember
    from app.models.manager import Manager


class TgAccountStatus(str, enum.Enum):
    active = "active"
    banned = "banned"
    offline = "offline"


class TgAccount(Base, TimestampMixin):
    __tablename__ = "tg_accounts"
    __table_args__ = (
        # Один менеджер может иметь по аккаунту в КАЖДОМ канале (TG + MAX),
        # но не два в одном.
        UniqueConstraint("manager_id", "messenger", name="uq_tg_accounts_manager_messenger"),
        # Телефон уникален внутри канала: номер менеджера может быть привязан
        # и к TG, и к MAX одновременно (одноимённые личности в двух мессенджерах).
        UniqueConstraint("messenger", "phone", name="uq_tg_accounts_messenger_phone"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    messenger: Mapped[Messenger] = mapped_column(
        Enum(Messenger), default=Messenger.tg, nullable=False
    )
    phone: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    # TG: путь к .session-файлу (исторически; фактический путь вычисляется
    # конвенцией account_<id>). MAX: NULL.
    session_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # MAX: сессия web-клиента (QR-онбординг). TG: NULL.
    token: Mapped[str | None] = mapped_column(Text, nullable=True)
    device_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # MAX: собственный user_id аккаунта (фильтр self-эхо). TG: NULL.
    max_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Отображаемое имя владельца MAX-аккаунта (из профиля).
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[TgAccountStatus] = mapped_column(
        Enum(TgAccountStatus), default=TgAccountStatus.offline
    )
    manager_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("managers.id"), nullable=False
    )

    manager: Mapped["Manager"] = relationship(back_populates="tg_accounts")
    # Состав линии (M:N): личный номер — один участник, общий — несколько.
    # manager_id — legacy-владелец, дублирует единственного участника до
    # уборки self-service онбординга (Этап 6 из плана линий).
    members: Mapped[list["AccountMember"]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )
