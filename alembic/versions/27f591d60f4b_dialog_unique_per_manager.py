"""dialog unique per manager

Revision ID: 27f591d60f4b
Revises: 7f79d9761e13
Create Date: 2026-08-14 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '27f591d60f4b'
down_revision: Union[str, None] = '7f79d9761e13'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Мультиаккаунт: в приватных TG-чатах external_chat_id == tg-id клиента
    # и совпадает у всех менеджеров, поэтому уникальна пара
    # (external_chat_id, assigned_user_id), а не chat_id сам по себе.
    #
    # SQL ниже — только postgres (DELETE ... USING, UPDATE ... FROM):
    # миграции выполняются только на VM; тесты используют create_all, не alembic.
    #
    # 1. Дедуп legacy-данных: до этой миграции upsert искал диалог только по
    #    external_chat_id, и гонка двух первых сообщений создавала дубли пары.
    #    Зависимые строки (messages, outbox; FK без ON DELETE) переносим на
    #    старейший диалог пары (MIN(id)) — иначе DELETE упадёт на FK при
    #    первом же дубле с сообщениями. Дедуп НЕОБРАТИМ: crm_deal_id/статус
    #    младших дублей теряются, остаются значения выжившего диалога.
    op.execute("""
        UPDATE messages m
        SET dialog_id = keep.survivor_id
        FROM (
            SELECT d.id AS dup_id,
                   MIN(d.id) OVER (
                       PARTITION BY d.external_chat_id, d.assigned_user_id
                   ) AS survivor_id
            FROM dialogs d
        ) AS keep
        WHERE m.dialog_id = keep.dup_id AND keep.dup_id <> keep.survivor_id
    """)
    op.execute("""
        UPDATE outbox o
        SET dialog_id = keep.survivor_id
        FROM (
            SELECT d.id AS dup_id,
                   MIN(d.id) OVER (
                       PARTITION BY d.external_chat_id, d.assigned_user_id
                   ) AS survivor_id
            FROM dialogs d
        ) AS keep
        WHERE o.dialog_id = keep.dup_id AND keep.dup_id <> keep.survivor_id
    """)
    op.execute("""
        DELETE FROM dialogs d USING dialogs d2
        WHERE d.external_chat_id = d2.external_chat_id
          AND d.assigned_user_id IS NOT DISTINCT FROM d2.assigned_user_id
          AND d.id > d2.id
    """)
    # 2. Констрейнт (он же даёт составной индекс — отдельный не нужен).
    op.create_unique_constraint(
        'uq_dialogs_chat_per_manager', 'dialogs',
        ['external_chat_id', 'assigned_user_id']
    )


def downgrade() -> None:
    # Внимание: дедуп в upgrade необратим — downgrade только снимает
    # констрейнт, удалённые дубли не восстанавливаются.
    op.drop_constraint('uq_dialogs_chat_per_manager', 'dialogs', type_='unique')
