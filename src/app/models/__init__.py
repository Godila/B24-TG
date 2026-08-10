"""Все ORM-модели приложения.

Импортируем модели здесь, чтобы ``Base.metadata`` увидел все таблицы и
SQLAlchemy смог разрешить строковые forward-refs в ``relationship()``
(например ``Mapped["TgAccount | None"]``). Внешним импортёрам достаточно
``from app.models import Base, Manager, ...``.
"""

from app.models.attachment import Attachment, AttachmentType
from app.models.base import Base, TimestampMixin
from app.models.contact import Contact
from app.models.dialog import Dialog, DialogStatus, Messenger
from app.models.manager import Manager, ManagerRole
from app.models.message import Message, MessageDirection, MessageStatus
from app.models.outbox import OutboxItem, OutboxStatus
from app.models.template import Template
from app.models.tg_account import TgAccount, TgAccountStatus

__all__ = [
    "Attachment",
    "AttachmentType",
    "Base",
    "Contact",
    "Dialog",
    "DialogStatus",
    "Manager",
    "ManagerRole",
    "Message",
    "MessageDirection",
    "MessageStatus",
    "Messenger",
    "OutboxItem",
    "OutboxStatus",
    "Template",
    "TgAccount",
    "TgAccountStatus",
    "TimestampMixin",
]
