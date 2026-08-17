"""attachments: file_name — оригинальное имя файла вложения

Revision ID: b5e8c2f7a913
Revises: a9d3f17c5e42
Create Date: 2026-08-14

Expand-only (nullable-столбец) — применяется без остановки сервиса.
Имя нужно для отображения в пузыре и Content-Disposition при раздаче;
у входящих TG-фото его нет (photo не несёт DocumentAttributeFilename) —
остаётся NULL, UI покажет тип вложения.
"""

revision = "b5e8c2f7a913"
down_revision = "a9d3f17c5e42"
branch_labels = None
depends_on = None

import sqlalchemy as sa

from alembic import op


def upgrade() -> None:
    op.add_column(
        "attachments",
        sa.Column("file_name", sa.String(255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("attachments", "file_name")
