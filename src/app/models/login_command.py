"""LoginCommand — команда онбординга/отвязки канального аккаунта (вариант B).

Одна строка = конечный автомат одного действия (QR-логин TG или log_out):
web пишет строку и читает статус, bridge (LoginCommandWorker) исполняет и
пишет переходы. 2FA-пароль проходит только транзитом (password_transit):
bridge стирает сразу после использования, TTL-чистка — зависшие.

Мессенджер-колонка канал-нейтральна: v1 строки только 'tg' (MAX-онбординг
живёт в web-процессе без команд), но таблица готова к 3-му каналу.
"""

import enum
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin
from app.models.dialog import Messenger


class LoginCommandKind(str, enum.Enum):
    qr_login = "qr_login"
    log_out = "log_out"


class LoginCommandStatus(str, enum.Enum):
    pending = "pending"                    # вставлена web'ом, bridge ещё не забрал
    waiting = "waiting"                    # QR выдан, ждём скана
    password_required = "password_required"  # bridge ждёт 2FA-пароль
    authorized = "authorized"              # QR-логин успешен (терминальный)
    done = "done"                          # log_out успешен (терминальный)
    expired = "expired"                    # QR-итерации исчерпаны / deadline
    cancelled = "cancelled"                # отменена (терминальный)
    error = "error"                        # сбой (терминальный)


#: Статусы «живой» команды — ровно одна такая на (manager, messenger).
ACTIVE_STATUSES = (
    LoginCommandStatus.pending,
    LoginCommandStatus.waiting,
    LoginCommandStatus.password_required,
)


class LoginCommand(Base, TimestampMixin):
    __tablename__ = "login_commands"
    __table_args__ = (
        # Не более одного живого логина на ЛИНИЮ (аккаунт) на уровне БД:
        # повторный старт терминализирует прежнюю строку в той же транзакции.
        Index(
            "uq_login_commands_active",
            "account_id",
            unique=True,
            postgresql_where=text(
                "status IN ('pending', 'waiting', 'password_required')"
            ),
            sqlite_where=text(
                "status IN ('pending', 'waiting', 'password_required')"
            ),
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    # ponytail: legacy-колонка инициатора (nullable, кодом не читается);
    # drop вместе с tg_accounts.manager_id в контракт-релизе.
    manager_id: Mapped[int | None] = mapped_column(
        ForeignKey("managers.id"), nullable=True, index=True
    )
    account_id: Mapped[int] = mapped_column(
        ForeignKey("tg_accounts.id"), nullable=False, index=True
    )
    messenger: Mapped[Messenger] = mapped_column(Enum(Messenger), nullable=False)
    kind: Mapped[LoginCommandKind] = mapped_column(
        Enum(LoginCommandKind), default=LoginCommandKind.qr_login, nullable=False
    )
    status: Mapped[LoginCommandStatus] = mapped_column(
        Enum(LoginCommandStatus),
        default=LoginCommandStatus.pending,
        nullable=False,
        index=True,
    )
    qr_link: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(String(512), nullable=True)
    #: 2FA-пароль ТОЛЬКО транзитом: bridge стирает при чтении, web — при
    #: отмене/TTL. Никогда не логируется и не попадает в API-ответы.
    password_transit: Mapped[str | None] = mapped_column(String(256), nullable=True)
    #: Число QR-итераций (wait → timeout → recreate).
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: Флаг отмены от web: bridge видит его в циклах ожидания и завершает.
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    deadline_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


async def terminate_active_commands(
    session: AsyncSession,
    *,
    status: LoginCommandStatus = LoginCommandStatus.cancelled,
    manager_id: int | None = None,
    account_id: int | None = None,
    messenger: Messenger | None = None,
) -> int:
    """Перевести живые LoginCommand в терминальный статус + стереть 2FA-пароль.

    Единственная точка инварианта uq_login_commands_active: терминализация
    обязана освобождать partial unique и вычищать password_transit. До
    выделения хелпера эта логика жила в четырёх копиях (start/cancel/
    unlink/selfheal) и начала расходиться стилем (enum vs сырые строки).
    Фильтры комбинируются; None — не фильтровать. Возвращает число строк.
    """
    stmt = update(LoginCommand).where(LoginCommand.status.in_(ACTIVE_STATUSES))
    if manager_id is not None:
        stmt = stmt.where(LoginCommand.manager_id == manager_id)
    if account_id is not None:
        stmt = stmt.where(LoginCommand.account_id == account_id)
    if messenger is not None:
        stmt = stmt.where(LoginCommand.messenger == messenger)
    result = await session.execute(
        stmt.values(status=status, password_transit=None)
    )
    return int(result.rowcount)
