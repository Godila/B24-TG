"""add b24_tokens table

Revision ID: 7f79d9761e13
Revises: 93cd8044d35d
Create Date: 2026-08-10 23:59:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7f79d9761e13'
down_revision: Union[str, None] = '93cd8044d35d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('b24_tokens',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('member_id', sa.String(length=64), nullable=False),
    sa.Column('access_token', sa.Text(), nullable=False),
    sa.Column('refresh_token', sa.Text(), nullable=False),
    sa.Column('client_endpoint', sa.String(length=255), nullable=False),
    sa.Column('portal', sa.String(length=255), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('scope', sa.Text(), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('member_id')
    )
    op.create_index(op.f('ix_b24_tokens_member_id'), 'b24_tokens', ['member_id'], unique=True)
    op.create_index(op.f('ix_b24_tokens_expires_at'), 'b24_tokens', ['expires_at'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_b24_tokens_expires_at'), table_name='b24_tokens')
    op.drop_index(op.f('ix_b24_tokens_member_id'), table_name='b24_tokens')
    op.drop_table('b24_tokens')
