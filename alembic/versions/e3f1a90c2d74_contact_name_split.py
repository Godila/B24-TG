"""contacts: раздельные first_name/last_name (для CRM NAME/LAST_NAME)

Revision ID: e3f1a90c2d74
Revises: a91c3f05d7b2
Create Date: 2026-08-16

Expand-only (два nullable-столбца) — применяется без остановки сервиса.
Отсутствие значений → CRM пишет отображаемое имя целиком в NAME (как было).
"""

revision = "e3f1a90c2d74"
down_revision = "a91c3f05d7b2"
branch_labels = None
depends_on = None

import sqlalchemy as sa  # noqa: E402
from alembic import op  # noqa: E402


def upgrade() -> None:
    op.add_column("contacts", sa.Column("first_name", sa.String(length=255), nullable=True))
    op.add_column("contacts", sa.Column("last_name", sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column("contacts", "last_name")
    op.drop_column("contacts", "first_name")
