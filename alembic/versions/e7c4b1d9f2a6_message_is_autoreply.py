"""Message.is_autoreply: маркер системных автоответов.

Счётчик «Ожидают ответа» (web/routes/inbox.py) считает неотвеченность как
«входящие после последнего исходящего» — автоответ-исходящий обязан из этого
предиката исключаться (семантика Wazzup: автоответ не снимает неотвеченность).

Revision ID: e7c4b1d9f2a6
Revises: b7d3e8f1a2c4
Create Date: 2026-08-20
"""

import sqlalchemy as sa

from alembic import op

revision = "e7c4b1d9f2a6"
down_revision = "b7d3e8f1a2c4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "messages",
        sa.Column(
            "is_autoreply", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )


def downgrade() -> None:
    op.drop_column("messages", "is_autoreply")
