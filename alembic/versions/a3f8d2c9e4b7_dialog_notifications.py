"""dialog_notifications: слоты feed-уведомлений (Wazzup-паритет).

Строка на (диалог, адресат): b24_message_id текущего сообщения в чате
адреса или NULL. UniqueConstraint держит «≤1 строки в фиде на диалог у
каждого адресата»; dismissed_at — маркер кнопки «Отвечать не нужно».

Revision ID: a3f8d2c9e4b7
Revises: c5e9a3f7b1d4
Create Date: 2026-08-22
"""

import sqlalchemy as sa

from alembic import op

revision = "a3f8d2c9e4b7"
down_revision = "c5e9a3f7b1d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dialog_notifications",
        sa.Column(
            "id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True
        ),
        sa.Column(
            "dialog_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            sa.ForeignKey("dialogs.id"),
            nullable=False,
        ),
        sa.Column("manager_b24_user_id", sa.Integer(), nullable=False),
        sa.Column("b24_message_id", sa.BigInteger(), nullable=True),
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint(
            "dialog_id",
            "manager_b24_user_id",
            name="uq_dialog_notifications_dialog_manager",
        ),
        # Отдельный индекс по dialog_id не нужен: уникальный констрейнт
        # выше уже даёт индекс с dialog_id ведущей колонкой.
    )


def downgrade() -> None:
    op.drop_table("dialog_notifications")
