"""Линии (Этап 2/6): участники линий + колонка-линия у диалогов.

Таблица account_members (M:N аккаунт↔менеджер с ролью participant/observer),
диалогам — account_id (линия, через которую идёт переписка) и уникальность
(chat, messenger, account_id): у общих линий ответственного нет (NULL в
старом ключе не дедуплицируется), а один клиент может писать на разные
линии. Backfill: владелец аккаунта → единственный участник; диалог → линия
владельца (личные аккаунты 1:1 по uq_tg_accounts_manager_messenger).

Expand-only для текущего кода: новые объекты им не читаются. Кнопки
управления составом линий появляются в панели этим же релизом.

⚠️ Порядок на живом проде — как у c3a7f1d92e40: применить сразу после
деплоя нового кода (окно в секунды) либо stop → upgrade → up.

Revision ID: f4b1d9c2a7e6
Revises: b5e8c2f7a913
Create Date: 2026-08-19

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "f4b1d9c2a7e6"
down_revision = "b5e8c2f7a913"
branch_labels = None
depends_on = None

linerole_t = postgresql.ENUM("participant", "observer", name="linerole")


def upgrade() -> None:
    op.create_table(
        "account_members",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id"), nullable=False
        ),
        sa.Column(
            "manager_id", sa.Integer(), sa.ForeignKey("managers.id"), nullable=False
        ),
        sa.Column("role", linerole_t, nullable=False, server_default="participant"),
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
        sa.UniqueConstraint("account_id", "manager_id", name="uq_account_members_pair"),
    )
    op.create_index(op.f("ix_account_members_account_id"), "account_members", ["account_id"])
    op.create_index(op.f("ix_account_members_manager_id"), "account_members", ["manager_id"])

    # Личный номер: владелец аккаунта — его первый участник.
    op.execute(
        "INSERT INTO account_members (account_id, manager_id, role, created_at, updated_at) "
        "SELECT id, manager_id, 'participant', now(), now() FROM tg_accounts"
    )

    op.add_column(
        "dialogs",
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id"), nullable=True),
    )
    op.create_index(op.f("ix_dialogs_account_id"), "dialogs", ["account_id"])
    # PG-синтаксис UPDATE...FROM; join однозначен (см. докстринг).
    op.execute(
        "UPDATE dialogs d SET account_id = a.id FROM tg_accounts a "
        "WHERE a.manager_id = d.assigned_user_id AND a.messenger = d.messenger"
    )
    op.create_unique_constraint(
        "uq_dialogs_chat_per_account",
        "dialogs",
        ["external_chat_id", "messenger", "account_id"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_dialogs_chat_per_account", "dialogs", type_="unique")
    op.drop_column("dialogs", "account_id")
    op.drop_table("account_members")
    linerole_t.drop(op.get_bind(), checkfirst=True)
