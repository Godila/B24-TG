"""Диалог между контактом и (опционально) назначенным менеджером в мессенджере."""

import enum
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.contact import Contact
    from app.models.message import Message


class Messenger(str, enum.Enum):
    tg = "tg"
    max = "max"


class DialogStatus(str, enum.Enum):
    active = "active"
    archived = "archived"


class Dialog(Base, TimestampMixin):
    __tablename__ = "dialogs"
    # Мультиаккаунт: в приватных TG-чатах external_chat_id == tg-id клиента
    # и совпадает у всех менеджеров, поэтому уникальна пара
    # (external_chat_id, assigned_user_id), а не chat_id сам по себе.
    # Мультиканал: id-пространства TG и MAX независимы и могут совпасть
    # строкой — поэтому в ключ добавлен messenger.
    # Констрейнт создаёт и составной индекс — отдельный не нужен.
    __table_args__ = (
        # Идентичность диалога — ЛИНИЯ (аккаунт): у общих линий ответственного
        # может не быть, а один клиент может писать на разные линии. Ключ по
        # (chat, assigned) убран: он запрещал два личных номера одного
        # менеджера в одном канале вести переписку с одним клиентом.
        UniqueConstraint(
            "external_chat_id",
            "messenger",
            "account_id",
            name="uq_dialogs_chat_per_account",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    contact_id: Mapped[int] = mapped_column(ForeignKey("contacts.id"), nullable=False, index=True)
    messenger: Mapped[Messenger] = mapped_column(Enum(Messenger), nullable=False)
    external_chat_id: Mapped[str] = mapped_column(String(128), nullable=False)
    # Линия (аккаунт), через которую идёт переписка: у личных линий
    # совпадает с аккаунтом владельца, у общих — единственная связь
    # диалога с номером (отправка/квитанции/видимость участников).
    account_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("tg_accounts.id"), nullable=True, index=True
    )
    # id CRM-сущности диалога; тип — в crm_entity_type ('deal'|'lead').
    # Колонка исторически называется crm_deal_id — переименование не
    # окупается (дискриминирующая пара и так контракт с первых ревизий).
    crm_deal_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    crm_entity_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    assigned_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[DialogStatus] = mapped_column(Enum(DialogStatus), default=DialogStatus.active)
    last_msg_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # ponytail: legacy-курсор владельца (пер-менеджерные — в dialog_reads
    # с Этапа 3; колонка не пишется и не читается) — drop в контракт-релизе.
    last_read_msg_id: Mapped[int | None] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), nullable=True
    )

    contact: Mapped["Contact"] = relationship(back_populates="dialogs")
    messages: Mapped[list["Message"]] = relationship(back_populates="dialog")
