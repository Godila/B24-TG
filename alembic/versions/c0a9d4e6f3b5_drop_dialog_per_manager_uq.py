"""Линии (Этап 6, safety-часть): убрать вредный ключ диалога по ответственному.

uq_dialogs_chat_per_manager (chat, assigned, messenger) остался от
пер-менеджерной модели и теперь ВРЕДЕН: два личных номера одного
менеджера (обе линии с единственным участником) не могли бы вести
переписку с одним и тем же клиентом — INSERT второго диалога падал бы.
Идентичность диалога полностью определяет ключ по линии
uq_dialogs_chat_per_account (Этап 2).

Полная контракт-фаза (drop tg_accounts.manager_id, login_commands.manager_id,
dialogs.last_read_msg_id) — отдельным релизом: колонки nullable и кодом
не читаются, дроп чисто механический.

Revision ID: c0a9d4e6f3b5
Revises: b9e4a7c1d3f8
Create Date: 2026-08-19

"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "c0a9d4e6f3b5"
down_revision = "b9e4a7c1d3f8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("uq_dialogs_chat_per_manager", "dialogs", type_="unique")


def downgrade() -> None:
    op.create_unique_constraint(
        "uq_dialogs_chat_per_manager",
        "dialogs",
        ["external_chat_id", "assigned_user_id", "messenger"],
    )
