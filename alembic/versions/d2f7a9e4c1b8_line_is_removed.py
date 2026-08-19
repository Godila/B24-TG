"""Линии: мягкое удаление (tg_accounts.is_removed).

DELETE /admin/api/lines/{id} скрывает отключённую линию из панели:
гасит логины/share-ссылки, чистит участников и креды; строка аккаунта и
диалоги остаются (FK сообщений/outbox, история у ответственных).
Expand-only: старый код флаг не читает.

Revision ID: d2f7a9e4c1b8
Revises: c0a9d4e6f3b5
Create Date: 2026-08-19

"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "d2f7a9e4c1b8"
down_revision = "c0a9d4e6f3b5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tg_accounts",
        sa.Column("is_removed", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("tg_accounts", "is_removed")
