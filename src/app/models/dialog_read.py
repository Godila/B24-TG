"""Курсор прочтения диалога конкретным менеджером.

Непрочитанные — состояние наблюдателя, а не диалога: у общего номера
(линии) каждый участник гасит свой бейдж независимо; supervisor носит
собственный курсор (раньше «читал» курсор владельца). Курсор идёт только
вперёд; пустой диалог курсора не имеет.
"""


from sqlalchemy import BigInteger, ForeignKey, Integer, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class DialogRead(Base, TimestampMixin):
    __tablename__ = "dialog_reads"
    __table_args__ = (
        # Курсор менеджера в диалоге — один.
        UniqueConstraint("dialog_id", "manager_id", name="uq_dialog_reads_pair"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dialog_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("dialogs.id"), nullable=False, index=True
    )
    manager_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("managers.id"), nullable=False, index=True
    )
    last_read_msg_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), nullable=False, default=0
    )
