"""Открытые линии B24 (imconnector): привязка аккаунта + im-пары сообщений.

tg_accounts: ol_line_id (линия контакт-центра; NULL = классический режим,
unique — одна линия B24 не обслуживается двумя аккаунтами) и ol_active.
messages: b24_im_chat_id / b24_im_message_id — im-пара события
ONIMCONNECTORMESSAGEADD для imconnector.send.status.delivery (прецедент —
timeline_comment_id). b24_tokens: application_token (сильная проверка
вебхуков коннектора; OAuth-токены в событиях опциональны).

Expand-only: колонки nullable/с дефолтом, существующий код их не читает
до релиза коннектора этим же деплоем.

Revision ID: e5c8a2f4b7d1
Revises: d2f7a9e4c1b8
Create Date: 2026-08-20

"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "e5c8a2f4b7d1"
down_revision = "d2f7a9e4c1b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tg_accounts", sa.Column("ol_line_id", sa.String(length=64), nullable=True))
    op.create_unique_constraint("uq_tg_accounts_ol_line_id", "tg_accounts", ["ol_line_id"])
    op.add_column(
        "tg_accounts",
        sa.Column("ol_active", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("messages", sa.Column("b24_im_chat_id", sa.BigInteger(), nullable=True))
    op.add_column("messages", sa.Column("b24_im_message_id", sa.BigInteger(), nullable=True))
    op.create_index("ix_messages_b24_im_message_id", "messages", ["b24_im_message_id"])
    # Дедуп событий коннектора на уровне БД (гонка двух доставок одного
    # события закрыта unique; NULL у не-операторских сообщений не участвует).
    _im_not_null = sa.text("b24_im_message_id IS NOT NULL")
    op.create_index(
        "uq_messages_dialog_b24_im_message",
        "messages",
        ["dialog_id", "b24_im_message_id"],
        unique=True,
        postgresql_where=_im_not_null,
        sqlite_where=_im_not_null,
    )
    op.add_column("b24_tokens", sa.Column("application_token", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("b24_tokens", "application_token")
    op.drop_index("ix_messages_b24_im_message_id", table_name="messages")
    op.drop_index("uq_messages_dialog_b24_im_message", table_name="messages")
    op.drop_column("messages", "b24_im_message_id")
    op.drop_column("messages", "b24_im_chat_id")
    op.drop_column("tg_accounts", "ol_active")
    op.drop_constraint("uq_tg_accounts_ol_line_id", "tg_accounts", type_="unique")
    op.drop_column("tg_accounts", "ol_line_id")
