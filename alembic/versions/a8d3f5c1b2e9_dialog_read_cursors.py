"""Линии (Этап 3/6): пер-участниковые курсоры прочтения (dialog_reads).

Курсор «владельца» из поля строки dialogs.last_read_msg_id разъезжается в
таблицу (dialog_id, manager_id): у общего номера каждый участник гасит
свой бейдж; supervisor теперь носит собственный курсор. Поле строки
перестаёт писаться кодом (contract-фаза уберёт колонку — Этап 6).

Expand-only: старый код не знает таблицы, новый перестаёт читать колонку.

Revision ID: a8d3f5c1b2e9
Revises: f4b1d9c2a7e6
Create Date: 2026-08-19

"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "a8d3f5c1b2e9"
down_revision = "f4b1d9c2a7e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dialog_reads",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("dialog_id", sa.Integer(), sa.ForeignKey("dialogs.id"), nullable=False),
        sa.Column("manager_id", sa.Integer(), sa.ForeignKey("managers.id"), nullable=False),
        sa.Column(
            "last_read_msg_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("dialog_id", "manager_id", name="uq_dialog_reads_pair"),
    )
    op.create_index(op.f("ix_dialog_reads_dialog_id"), "dialog_reads", ["dialog_id"])
    op.create_index(op.f("ix_dialog_reads_manager_id"), "dialog_reads", ["manager_id"])
    # Курсор владельца уезжает с ним; NULL-курсоры (никогда не открывал)
    # строк не создают — «не открывал» = «всё непрочитано» и в новой схеме.
    op.execute(
        "INSERT INTO dialog_reads (dialog_id, manager_id, last_read_msg_id, created_at, updated_at) "
        "SELECT id, assigned_user_id, last_read_msg_id, now(), now() FROM dialogs "
        "WHERE assigned_user_id IS NOT NULL AND last_read_msg_id IS NOT NULL"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE dialogs d SET last_read_msg_id = r.last_read_msg_id "
        "FROM dialog_reads r "
        "WHERE r.dialog_id = d.id AND r.manager_id = d.assigned_user_id"
    )
    op.drop_table("dialog_reads")
