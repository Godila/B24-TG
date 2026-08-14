"""outbox.message_id link to messages

Revision ID: 3d9a71c2e8b4
Revises: 27f591d60f4b
Create Date: 2026-08-14 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3d9a71c2e8b4'
down_revision: Union[str, None] = '27f591d60f4b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Замыкание исходящего TG-цикла: OutboxItem ссылается на Message,
    # чтобы после отправки воркер обновлял Message.status/tg_message_id.
    # nullable=True — исторические строки outbox без связи остаются валидными.
    # Миграции выполняются только на VM (postgres); тесты используют create_all.
    op.add_column('outbox', sa.Column('message_id', sa.BigInteger(), nullable=True))
    op.create_index('ix_outbox_message_id', 'outbox', ['message_id'])
    op.create_foreign_key(
        'fk_outbox_message', 'outbox', 'messages', ['message_id'], ['id']
    )


def downgrade() -> None:
    # В обратном порядке: сначала FK, затем индекс, затем столбец.
    op.drop_constraint('fk_outbox_message', 'outbox', type_='foreignkey')
    op.drop_index('ix_outbox_message_id', table_name='outbox')
    op.drop_column('outbox', 'message_id')
