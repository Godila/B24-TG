"""Все ORM-модели приложения.

Импортируем модели здесь, чтобы ``Base.metadata`` увидел все таблицы и
SQLAlchemy смог разрешить строковые forward-refs в ``relationship()``
(например ``Mapped["TgAccount | None"]``). Внешним импортёрам достаточно
``from app.models import Base, Manager, ...``.
"""

from app.models.attachment import Attachment, AttachmentType
from app.models.b24_token import B24Token
from app.models.base import Base, TimestampMixin
from app.models.contact import Contact
from app.models.crm_sync import CrmSyncItem, CrmSyncStatus, KIND_INBOUND, KIND_OUTBOUND
from app.models.dialog import Dialog, DialogStatus, Messenger
from app.models.manager import Manager, ManagerRole
from app.models.message import Message, MessageDirection, MessageStatus
from app.models.outbox import OutboxItem, OutboxStatus
from app.models.template import Template
from app.models.tg_account import TgAccount, TgAccountStatus

__all__ = [
    "Attachment",
    "AttachmentType",
    "B24Token",
    "Base",
    "Contact",
    "CrmSyncItem",
    "CrmSyncStatus",
    "Dialog",
    "DialogStatus",
    "KIND_INBOUND",
    "KIND_OUTBOUND",
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
