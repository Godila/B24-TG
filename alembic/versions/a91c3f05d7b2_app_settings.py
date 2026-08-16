"""app_settings: глобальные настройки (timeline_mode и пр.)

Revision ID: a91c3f05d7b2
Revises: d8e2b6a91c74
Create Date: 2026-08-16

Expand-only (новая таблица) — применяется без остановки сервиса.
`timeline_mode` отсутствует в таблице → код использует дефолт "first".
"""

revision = "a91c3f05d7b2"
down_revision = "d8e2b6a91c74"
branch_labels = None
depends_on = None

import sqlalchemy as sa  # noqa: E402
from alembic import op  # noqa: E402


def upgrade() -> None:
    op.create_table(
        "app_settings",
        sa.Column("key", sa.String(length=64), primary_key=True),
        sa.Column("value", sa.String(length=255), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
    )


def downgrade() -> None:
    op.drop_table("app_settings")
