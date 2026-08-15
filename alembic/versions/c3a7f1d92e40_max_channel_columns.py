"""MAX-канал: канал-колонки и канальные уникальности.

Гибридная схема (одобрено 2026-08-15): таблица tg_accounts НЕ переименовывается,
но получает messenger + MAX-креды; contacts/messages получают честные
external_id String; уникальности становятся составными с каналом.

⚠️ Порядок применения на живом проде (ренейм колонок ломает старый код):
    docker compose stop web bridge
    docker compose run --rm web alembic upgrade head
    docker compose up -d --build
(или: применить СРАЗУ после деплоя нового кода — окно в секунды, очереди
outbox/crm_sync переживут его ретраями; для личного инструмента допустимо
оба варианта, первый — без окна вовсе).

Все существующие строки — канал 'tg' (в проде других нет), backfill тривиален.

Revision ID: c3a7f1d92e40
Revises: b41d7c93e5a2
Create Date: 2026-08-15

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "c3a7f1d92e40"
down_revision = "b41d7c93e5a2"
branch_labels = None
depends_on = None

# PG-тип messenger ('tg' | 'max') уже существует (создан initial-схемой для
# dialogs) — переиспользуем, не создаём дубликат.
messenger_t = postgresql.ENUM("tg", "max", name="messenger", create_type=False)


def upgrade() -> None:
    # --- tg_accounts: канал + MAX-креды ---
    op.add_column(
        "tg_accounts",
        sa.Column("messenger", messenger_t, nullable=False, server_default="tg"),
    )
    op.add_column("tg_accounts", sa.Column("token", sa.Text(), nullable=True))
    op.add_column("tg_accounts", sa.Column("device_id", sa.String(64), nullable=True))
    op.add_column(
        "tg_accounts", sa.Column("max_user_id", sa.BigInteger(), nullable=True)
    )
    op.add_column(
        "tg_accounts", sa.Column("display_name", sa.String(255), nullable=True)
    )
    op.alter_column(
        "tg_accounts", "session_path", existing_type=sa.String(512), nullable=True
    )
    op.drop_column("tg_accounts", "last_floodwait_at")  # мёртвая колонка (никогда не читалась)
    op.drop_index(op.f("ix_tg_accounts_phone"), table_name="tg_accounts")
    op.create_index(
        "ix_tg_accounts_phone", "tg_accounts", ["phone"], unique=False
    )
    op.drop_constraint(
        "tg_accounts_manager_id_key", "tg_accounts", type_="unique"
    )
    op.create_unique_constraint(
        "uq_tg_accounts_manager_messenger",
        "tg_accounts",
        ["manager_id", "messenger"],
    )
    op.create_unique_constraint(
        "uq_tg_accounts_messenger_phone",
        "tg_accounts",
        ["messenger", "phone"],
    )

    # --- contacts: канальная идентичность (messenger, external_user_id) ---
    op.add_column(
        "contacts",
        sa.Column("messenger", messenger_t, nullable=False, server_default="tg"),
    )
    op.add_column(
        "contacts", sa.Column("external_user_id", sa.String(64), nullable=True)
    )
    op.execute("UPDATE contacts SET external_user_id = tg_user_id::text")
    op.alter_column("contacts", "external_user_id", nullable=False)
    op.drop_index(op.f("ix_contacts_tg_user_id"), table_name="contacts")
    op.drop_column("contacts", "tg_user_id")
    op.create_index(
        "ix_contacts_external_user_id", "contacts", ["external_user_id"], unique=False
    )
    op.create_unique_constraint(
        "uq_contacts_messenger_external_user_id",
        "contacts",
        ["messenger", "external_user_id"],
    )

    # --- dialogs: ключ уникальности += messenger (ослабление: дедуп не нужен) ---
    op.drop_constraint("uq_dialogs_chat_per_manager", "dialogs", type_="unique")
    op.create_unique_constraint(
        "uq_dialogs_chat_per_manager",
        "dialogs",
        ["external_chat_id", "assigned_user_id", "messenger"],
    )

    # --- messages: внешний id строкой (id MAX длинные; Bot API mid строковый) ---
    op.alter_column(
        "messages",
        "tg_message_id",
        existing_type=sa.BigInteger(),
        type_=sa.String(64),
        postgresql_using="tg_message_id::text",
    )
    op.drop_index(op.f("ix_messages_tg_message_id"), table_name="messages")
    op.alter_column("messages", "tg_message_id", new_column_name="external_message_id")
    op.create_index(
        "ix_messages_external_message_id",
        "messages",
        ["external_message_id"],
        unique=False,
    )


def downgrade() -> None:
    # ВНИМАНИЕ: обратный ход возможен только после ручного удаления
    # MAX-строк (tg_accounts.messenger='max', контакты/диалоги/сообщения
    # канала max): восстановление NOT NULL session_path и одиночных
    # UNIQUE(manager_id)/UNIQUE(phone) падает на менеджерах с аккаунтами
    # в обоих каналах. Внешние id обоих каналов числовые — конверсия
    # external_message_id/external_user_id обратно в bigint безопасна.
    op.drop_index(
        op.f("ix_messages_external_message_id"), table_name="messages"
    )
    op.alter_column(
        "messages", "external_message_id", new_column_name="tg_message_id"
    )
    op.alter_column(
        "messages",
        "tg_message_id",
        existing_type=sa.String(64),
        type_=sa.BigInteger(),
        postgresql_using="tg_message_id::bigint",
    )
    op.create_index(
        op.f("ix_messages_tg_message_id"), "messages", ["tg_message_id"], unique=False
    )

    op.drop_constraint("uq_dialogs_chat_per_manager", "dialogs", type_="unique")
    op.create_unique_constraint(
        "uq_dialogs_chat_per_manager",
        "dialogs",
        ["external_chat_id", "assigned_user_id"],
    )

    op.drop_constraint(
        "uq_contacts_messenger_external_user_id", "contacts", type_="unique"
    )
    op.drop_index(op.f("ix_contacts_external_user_id"), table_name="contacts")
    op.add_column(
        "contacts",
        sa.Column("tg_user_id", sa.BigInteger(), nullable=True),
    )
    op.execute("UPDATE contacts SET tg_user_id = external_user_id::bigint")
    op.alter_column("contacts", "tg_user_id", nullable=False)
    op.create_index(
        op.f("ix_contacts_tg_user_id"), "contacts", ["tg_user_id"], unique=True
    )
    op.drop_column("contacts", "external_user_id")
    op.drop_column("contacts", "messenger")

    op.drop_constraint(
        "uq_tg_accounts_messenger_phone", "tg_accounts", type_="unique"
    )
    op.drop_constraint(
        "uq_tg_accounts_manager_messenger", "tg_accounts", type_="unique"
    )
    op.create_unique_constraint("tg_accounts_manager_id_key", "tg_accounts", ["manager_id"])
    op.drop_index(op.f("ix_tg_accounts_phone"), table_name="tg_accounts")
    op.create_index(
        op.f("ix_tg_accounts_phone"), "tg_accounts", ["phone"], unique=True
    )
    op.add_column(
        "tg_accounts",
        sa.Column("last_floodwait_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.drop_column("tg_accounts", "display_name")
    op.drop_column("tg_accounts", "max_user_id")
    op.drop_column("tg_accounts", "device_id")
    op.drop_column("tg_accounts", "token")
    op.drop_column("tg_accounts", "messenger")
    # session_path снова NOT NULL — только теперь, когда MAX-строк нет.
    op.alter_column(
        "tg_accounts", "session_path", existing_type=sa.String(512), nullable=False
    )
