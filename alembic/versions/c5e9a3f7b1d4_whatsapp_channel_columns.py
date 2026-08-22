"""WhatsApp-канал: значение enum messenger + WA-колонки tg_accounts.

PG-enum messenger расширяется ALTER TYPE … ADD VALUE — это нельзя делать
внутри транзакции, поэтому autocommit-блок. Новых NOT NULL и переименований
нет — stop-окна не требуется: применяем СНАЧАЛА миграцию, потом образы
(старый код значения 'wa' не пишет; новый без колонок не стартует).

Downgrade: колонки падают тривиально; значение enum 'wa' PG удалить не
умеет — обратный ход через пересоздание типа вручную (строк 'wa' в БД
быть не должно, см. c3a7f1d92e40 о том же для 'max').

Revision ID: c5e9a3f7b1d4
Revises: e7c4b1d9f2a6
Create Date: 2026-08-22

"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "c5e9a3f7b1d4"
down_revision = "e7c4b1d9f2a6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE messenger ADD VALUE IF NOT EXISTS 'wa'")
    op.add_column("tg_accounts", sa.Column("wa_session_id", sa.String(64), nullable=True))
    op.add_column("tg_accounts", sa.Column("restriction_kind", sa.String(32), nullable=True))
    op.add_column(
        "tg_accounts", sa.Column("restriction_until", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("tg_accounts", "restriction_until")
    op.drop_column("tg_accounts", "restriction_kind")
    op.drop_column("tg_accounts", "wa_session_id")
    # значение enum 'wa' остаётся в типе (см. докстринг)
