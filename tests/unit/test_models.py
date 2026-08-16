"""Тесты ORM-моделей: создание таблиц + ключевые связи и поля.

Проверяем, что ``Base.metadata`` видит все 8 таблиц из спеки, что связь
Manager ↔ TgAccount реализована как 1:1 (unique FK), и что у ``outbox`` есть
все поля, необходимые для OutboxWorker (Task 11), включая ``is_initiation``
и ``external_chat_id`` из замечаний self-review плана.
"""

import pytest
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine

from app.models import Base, OutboxItem, TgAccount


@pytest.mark.asyncio
async def test_models_create_tables(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with engine.connect() as conn:
        tables = await conn.run_sync(lambda c: inspect(c).get_table_names())

    for name in [
        "managers",
        "tg_accounts",
        "contacts",
        "dialogs",
        "messages",
        "attachments",
        "outbox",
        "templates",
    ]:
        assert name in tables
    await engine.dispose()


def test_manager_one_account_per_channel():
    """Уникальность (manager_id, messenger): менеджер может иметь аккаунт в
    каждом канале, но не два в одном; phone уникален внутри канала."""
    uq = {c.name for c in TgAccount.__table__.constraints if hasattr(c, "name")}
    assert "uq_tg_accounts_manager_messenger" in uq
    assert "uq_tg_accounts_messenger_phone" in uq
    assert TgAccount.__table__.c.manager_id.unique is not True
    cols = {c.name for c in TgAccount.__table__.columns}
    assert "messenger" in cols
    assert "token" in cols
    assert "device_id" in cols
    assert "max_user_id" in cols


def test_outbox_has_required_status_fields():
    cols = {c.name for c in OutboxItem.__table__.columns}
    for required in ("status", "attempts", "next_attempt_at", "tg_account_id"):
        assert required in cols


def test_outbox_has_initiation_and_chat_id_fields():
    cols = {c.name for c in OutboxItem.__table__.columns}
    assert "is_initiation" in cols
    assert "external_chat_id" in cols


def test_dialog_has_last_read_msg_id():
    """Курсор непрочитанных владельца (общий мессенджер): nullable, без
    индекса (читается только по PK диалога)."""
    from app.models import Dialog

    col = Dialog.__table__.c.last_read_msg_id
    assert col.nullable is True
    assert col.index is not True
