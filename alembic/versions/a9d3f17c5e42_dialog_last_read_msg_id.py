"""dialogs: курсор прочтения last_read_msg_id (общий мессенджер)

Revision ID: a9d3f17c5e42
Revises: e3f1a90c2d74
Create Date: 2026-08-14

Expand-only (nullable-столбец) — применяется без остановки сервиса.
Backfill: курсор = MAX(messages.id) на момент миграции — вся история до
развёртывания «Чатов» считается прочитанной, иначе роллаут зальёт бейджи
непрочитанных за всё время. Неотвеченные считаются на лету из направлений
сообщений — им backfill не нужен.
"""

revision = "a9d3f17c5e42"
down_revision = "e3f1a90c2d74"
branch_labels = None
depends_on = None

import sqlalchemy as sa  # noqa: E402
from alembic import op  # noqa: E402


def upgrade() -> None:
    op.add_column(
        "dialogs",
        sa.Column("last_read_msg_id", sa.BigInteger(), nullable=True),
    )
    op.execute(
        "UPDATE dialogs SET last_read_msg_id = "
        "(SELECT MAX(id) FROM messages WHERE messages.dialog_id = dialogs.id)"
    )


def downgrade() -> None:
    op.drop_column("dialogs", "last_read_msg_id")
