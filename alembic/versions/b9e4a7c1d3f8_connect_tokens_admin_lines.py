"""Линии (Этап 5/6): share-ссылки подключения + админские линии без владельца.

Таблица connect_tokens (хэш токена, TTL, revoke, used), legacy-колонки
tg_accounts.manager_id и login_commands.manager_id становятся nullable —
линию создаёт админ без «призрачного владельца». Partial unique живых
логинов переезжает с пары (manager, messenger) на аккаунт (линию).

⚠️ Порядок на живом проде: применить сразу после деплоя нового кода
(окно в секунды) либо stop → upgrade → up (как у c3a7f1d92e40).

Revision ID: b9e4a7c1d3f8
Revises: a8d3f5c1b2e9
Create Date: 2026-08-19

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "b9e4a7c1d3f8"
down_revision = "a8d3f5c1b2e9"
branch_labels = None
depends_on = None

_ACTIVE_WHERE = "status IN ('pending', 'waiting', 'password_required')"


def upgrade() -> None:
    op.create_table(
        "connect_tokens",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id"), nullable=False
        ),
        sa.Column("messenger", postgresql.ENUM("tg", "max", name="messenger", create_type=False), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_by", sa.Integer(), sa.ForeignKey("managers.id"), nullable=False
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
    )
    op.create_index(op.f("ix_connect_tokens_account_id"), "connect_tokens", ["account_id"])

    op.alter_column(
        "tg_accounts", "manager_id", existing_type=sa.Integer(), nullable=True
    )
    op.alter_column(
        "login_commands", "manager_id", existing_type=sa.Integer(), nullable=True
    )
    # Ключ живого логина — линия (аккаунт).
    op.drop_index("uq_login_commands_active", table_name="login_commands")
    op.create_index(
        "uq_login_commands_active",
        "login_commands",
        ["account_id"],
        unique=True,
        postgresql_where=sa.text(_ACTIVE_WHERE),
        sqlite_where=sa.text(_ACTIVE_WHERE),
    )


def downgrade() -> None:
    op.drop_index("uq_login_commands_active", table_name="login_commands")
    op.create_index(
        "uq_login_commands_active",
        "login_commands",
        ["manager_id", "messenger"],
        unique=True,
        postgresql_where=sa.text(_ACTIVE_WHERE),
        sqlite_where=sa.text(_ACTIVE_WHERE),
    )
    op.execute(
        "UPDATE login_commands SET manager_id = "
        "(SELECT manager_id FROM tg_accounts a WHERE a.id = login_commands.account_id) "
        "WHERE manager_id IS NULL"
    )
    op.alter_column(
        "login_commands", "manager_id", existing_type=sa.Integer(), nullable=False
    )
    op.execute(
        "UPDATE tg_accounts SET manager_id = "
        "(SELECT manager_id FROM account_members m WHERE m.account_id = tg_accounts.id "
        " ORDER BY m.id LIMIT 1) WHERE manager_id IS NULL"
    )
    op.alter_column(
        "tg_accounts", "manager_id", existing_type=sa.Integer(), nullable=False
    )
    op.drop_table("connect_tokens")
