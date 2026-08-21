"""«Написать первым» из карточки CRM: команды инициации + приоритетный аккаунт.

initiations: web→bridge команда резолва (паттерн login_commands) — bridge
резолвит peer живым провайдером и создаёт Contact/Dialog/Message/OutboxItem.
managers.default_outbound: {"tg": id, "max": id} — приоритетный аккаунт
исходящих инициаций менеджера (селектор виджета, дефолт).

Expand-only: новая таблица + nullable-колонка, существующий код не читает
их до релиза фичи этим же деплоем.

Revision ID: b7d3e8f1a2c4
Revises: e5c8a2f4b7d1
Create Date: 2026-08-20

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "b7d3e8f1a2c4"
down_revision = "e5c8a2f4b7d1"
branch_labels = None
depends_on = None

messenger_t = postgresql.ENUM("tg", "max", name="messenger", create_type=False)

_PENDING = sa.text("status = 'pending'")


def upgrade() -> None:
    op.create_table(
        "initiations",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer, "sqlite"),
                  autoincrement=True, nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("messenger", messenger_t, nullable=False),
        sa.Column("author_manager_id", sa.Integer(), nullable=False),
        sa.Column("author_b24_user_id", sa.Integer(), nullable=False),
        sa.Column("entity_type", sa.String(length=16), nullable=True),
        sa.Column("entity_id", sa.Integer(), nullable=True),
        sa.Column("dest_kind", sa.String(length=16), nullable=False),
        sa.Column("dest_value", sa.String(length=128), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("status", sa.Enum("pending", "linked", "failed", name="initiationstatus"),
                  nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("last_error", sa.String(length=512), nullable=True),
        sa.Column("dialog_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["tg_accounts.id"]),
        sa.ForeignKeyConstraint(["author_manager_id"], ["managers.id"]),
        sa.ForeignKeyConstraint(["dialog_id"], ["dialogs.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_initiations_account_id", "initiations", ["account_id"])
    op.create_index("ix_initiations_author_manager_id", "initiations", ["author_manager_id"])
    op.create_index("ix_initiations_next_attempt_at", "initiations", ["next_attempt_at"])
    # Одна живая (pending) инициализация на (аккаунт, dest): дубль двойного
    # клика ловит БД, роут маппит IntegrityError → 409.
    op.create_index(
        "uq_initiations_active",
        "initiations",
        ["account_id", "dest_value"],
        unique=True,
        postgresql_where=_PENDING,
        sqlite_where=_PENDING,
    )
    op.add_column("managers", sa.Column("default_outbound", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("managers", "default_outbound")
    op.drop_index("uq_initiations_active", table_name="initiations")
    op.drop_index("ix_initiations_next_attempt_at", table_name="initiations")
    op.drop_index("ix_initiations_author_manager_id", table_name="initiations")
    op.drop_index("ix_initiations_account_id", table_name="initiations")
    op.drop_table("initiations")
