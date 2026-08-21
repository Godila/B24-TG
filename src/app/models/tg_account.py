"""Канальный аккаунт, привязанный к менеджеру (по одному на канал).

Таблица исторически называется ``tg_accounts`` (канал Telegram был первым);
с добавлением MAX в ней живут аккаунты обоих каналов — колонка ``messenger``
различает их, а уникальности составные: (messenger, manager_id) и
(messenger, phone). TG-строки используют ``session_path`` (файл Telethon),
MAX-строки — ``token``/``device_id`` (сессия web-клиента MAX).
"""

import enum

from sqlalchemy import (
    BigInteger,
    Boolean,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin
from app.models.dialog import Messenger


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
    #: Удалена из панели (история диалогов/FK остаются; см. DELETE /lines).
    is_removed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Открытая линия B24 (imconnector): id линии из контакт-центра, когда
    # аккаунт привязан к ней (слайдер коннектора в карточке линии). None =
    # классический режим (панель + наш CRM-синк). Unique — одна линия B24
    # не обслуживается двумя аккаунтами (NULL у непривязанных допустим).
    ol_line_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, unique=True
    )
    # Коннектор активен на линии (imconnector.activate); при False сообщения
    # копятся в очереди crm_sync и ждут реактивации.
    ol_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # ponytail: legacy-колонка владельца (nullable, кодом не читается);
    # drop отдельным контракт-релизом, если появится 3-й читатель схемы.
    manager_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("managers.id"), nullable=True
    )

