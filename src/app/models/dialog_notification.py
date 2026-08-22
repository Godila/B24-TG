"""Слот feed-уведомления диалога в чате приложения B24 (Wazzup-паритет).

Строка = «у адресата manager_b24_user_id висит сообщение b24_message_id в
его личном чате с приложением». b24_message_id IS NULL — показывать нечего
(отвечено/погашено/ещё не отправлено); UniqueConstraint(dialog, менеджер)
держит инвариант «≤1 строки в фиде на диалог у каждого адресата».

dismissed_at — маркер кнопки «Отвечать не нужно» (web-роут ставит, sweep
воркера подчищает сообщение). Контрол-флоу его НЕ читает: следующее
входящее рендерит уведомление заново по предикату неотвеченности.
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class DialogNotification(Base, TimestampMixin):
    __tablename__ = "dialog_notifications"
    __table_args__ = (
        UniqueConstraint(
            "dialog_id",
            "manager_b24_user_id",
            name="uq_dialog_notifications_dialog_manager",
        ),
    )

    # BigInteger на Postgres; на SQLite автоинкремент работает только для
    # INTEGER PRIMARY KEY, поэтому через variant используем Integer.
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    dialog_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("dialogs.id"),
        nullable=False,
    )
    # B24-идентичность адресата (DIALOG_ID im.message.add), не FK на
    # managers: строка Manager может отставать от состава линии портала.
    manager_b24_user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    # Текущее сообщение в чате адресата; NULL = в фиде ничего нет.
    b24_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    dismissed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
