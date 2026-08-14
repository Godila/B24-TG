"""crm_sync queue

Revision ID: b41d7c93e5a2
Revises: 3d9a71c2e8b4
Create Date: 2026-08-14 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b41d7c93e5a2'
down_revision: Union[str, None] = '3d9a71c2e8b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Очередь CRM-синхронизации (план 006): сообщение сохраняется в нашей БД
    # сразу, а CRM-записи (контакт/сделка/timeline) делает CrmSyncWorker
    # отсюда — с ретраями/backoff, как outbox. kind: 'inbound'|'outbound'.
    # Миграции выполняются только на VM (postgres); тесты используют create_all.
    op.create_table('crm_sync',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('message_id', sa.BigInteger(), nullable=False),
    sa.Column('status', sa.Enum('queued', 'done', 'failed', 'retrying', name='crmsyncstatus'), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('next_attempt_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('last_error', sa.String(length=512), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['message_id'], ['messages.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_crm_sync_message_id'), 'crm_sync', ['message_id'], unique=False)
    op.create_index('ix_crm_sync_status_next_attempt_at', 'crm_sync', ['status', 'next_attempt_at'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_crm_sync_status_next_attempt_at', table_name='crm_sync')
    op.drop_index(op.f('ix_crm_sync_message_id'), table_name='crm_sync')
    op.drop_table('crm_sync')
