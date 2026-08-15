"""Админ-панель v1: права read-only + таблица команд онбординга TG.

Только расширения (expand-safe): managers.is_readonly с server_default,
новая таблица login_commands с partial unique-индексом живых команд.
Прод-порядок прежний (см. c3a7f1d92e40): применяется любым способом до/
после деплоя нового кода — старый код новых объектов не знает.

Revision ID: d8e2b6a91c74
Revises: c3a7f1d92e40
Create Date: 2026-08-15

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "d8e2b6a91c74"
down_revision = "c3a7f1d92e40"
branch_labels = None
depends_on = None

messenger_t = postgresql.ENUM("tg", "max", name="messenger", create_type=False)

_ACTIVE_WHERE = "status IN ('pending', 'waiting', 'password_required')"


def upgrade() -> None:
    op.add_column(
        "managers",
        sa.Column("is_readonly", sa.Boolean(), nullable=False, server_default=sa.false()),
    )

    op.create_table(
        "login_commands",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer, "sqlite"),
                  autoincrement=True, nullable=False),
        sa.Column("manager_id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("messenger", messenger_t, nullable=False),
        sa.Column("kind", sa.Enum("qr_login", "log_out", name="logincommandkind"),
                  nullable=False),
        sa.Column("status", sa.Enum(
            "pending", "waiting", "password_required", "authorized",
            "done", "expired", "cancelled", "error", name="logincommandstatus"),
            nullable=False),
        sa.Column("qr_link", sa.Text(), nullable=True),
        sa.Column("error", sa.String(length=512), nullable=True),
        sa.Column("password_transit", sa.String(length=256), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["manager_id"], ["managers.id"]),
        sa.ForeignKeyConstraint(["account_id"], ["tg_accounts.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_login_commands_manager_id", "login_commands", ["manager_id"], unique=False
    )
    op.create_index(
        "ix_login_commands_account_id", "login_commands", ["account_id"], unique=False
    )
    op.create_index(
        "ix_login_commands_status", "login_commands", ["status"], unique=False
    )
    op.create_index(
        "uq_login_commands_active",
        "login_commands",
        ["manager_id", "messenger"],
        unique=True,
        postgresql_where=sa.text(_ACTIVE_WHERE),
        sqlite_where=sa.text(_ACTIVE_WHERE),
    )


def downgrade() -> None:
    op.drop_index("uq_login_commands_active", table_name="login_commands")
    op.drop_index("ix_login_commands_status", table_name="login_commands")
    op.drop_index("ix_login_commands_account_id", table_name="login_commands")
    op.drop_index("ix_login_commands_manager_id", table_name="login_commands")
    op.drop_table("login_commands")
    op.drop_column("managers", "is_readonly")
