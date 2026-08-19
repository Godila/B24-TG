"""Все ORM-модели приложения.

Импортируем модели здесь, чтобы ``Base.metadata`` увидел все таблицы и
SQLAlchemy смог разрешить строковые forward-refs в ``relationship()``
(например ``Mapped["TgAccount | None"]``). Внешним импортёрам достаточно
``from app.models import Base, Manager, ...``.
"""

from app.models.account_member import AccountMember, LineRole
from app.models.app_setting import AppSetting
from app.models.attachment import Attachment, AttachmentType
from app.models.b24_token import B24Token
from app.models.base import Base, TimestampMixin
from app.models.contact import Contact
from app.models.crm_sync import KIND_INBOUND, KIND_OUTBOUND, CrmSyncItem, CrmSyncStatus
from app.models.dialog import Dialog, DialogStatus, Messenger
from app.models.dialog_read import DialogRead
from app.models.login_command import (
    ACTIVE_STATUSES,
    LoginCommand,
    LoginCommandKind,
    LoginCommandStatus,
    terminate_active_commands,
)
from app.models.manager import Manager, ManagerRole
from app.models.message import Message, MessageDirection, MessageStatus
from app.models.outbox import OutboxItem, OutboxStatus
from app.models.template import Template
from app.models.tg_account import TgAccount, TgAccountStatus

__all__ = [
    "ACTIVE_STATUSES",
    "KIND_INBOUND",
    "KIND_OUTBOUND",
    "AccountMember",
    "AppSetting",
    "Attachment",
    "AttachmentType",
    "B24Token",
    "Base",
    "Contact",
    "CrmSyncItem",
    "CrmSyncStatus",
    "Dialog",
    "DialogRead",
    "DialogStatus",
    "LineRole",
    "LoginCommand",
    "LoginCommandKind",
    "LoginCommandStatus",
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
    "terminate_active_commands",
]
