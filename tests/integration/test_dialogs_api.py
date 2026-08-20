"""Интеграционные тесты /api/dialogs: список, история, отправка.

Эндпоинтам нужен DB + auth + Manager. Используем ``app.dependency_overrides``,
чтобы подменить ``get_session`` in-memory SQLite-сессией и
``get_current_manager`` — засеянным менеджером (без проверки куки).
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool


@pytest.fixture
async def app_with_data():
    """Поднимает in-memory SQLite, сеет данные, override'ит зависимости.

    Возвращает ``(TestClient, {ids...}, SessionLocal)``. Базовое окружение
    выставляет conftest (_hermetic_env).
    """
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        from app.models import Base

        await conn.run_sync(Base.metadata.create_all)
    SessionLocal = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )

    # Seed: manager (id=1, b24_user_id=15), tg_account, contact,
    # dialog (assigned to manager 1), inbound + outbound messages.
    from app.models import (
        Contact,
        Dialog,
        Manager,
        Message,
        MessageDirection,
        MessageStatus,
        Messenger,
        TgAccount,
    )

    ids: dict[str, int] = {}
    async with SessionLocal() as s:
        s.add(Manager(id=1, name="Иван", b24_user_id=15, is_active=True))
        s.add(TgAccount(id=7, messenger=Messenger.tg, phone="+79991234567", session_path="/tmp/s", manager_id=1))
        s.add(Contact(id=10, messenger=Messenger.tg, external_user_id="999", phone="+79990000001", name="Клиент"))
        s.add(
            Dialog(
                id=20,
                contact_id=10,
                messenger=Messenger.tg,
                external_chat_id="100200",
                assigned_user_id=1,
                crm_deal_id=42,
                status="active",
            )
        )
        s.add(
            Message(
                id=1,
                dialog_id=20,
                direction=MessageDirection.inbound,
                text="Привет",
                status=MessageStatus.delivered,
                external_message_id="111",
            )
        )
        s.add(
            Message(
                id=2,
                dialog_id=20,
                direction=MessageDirection.outbound,
                text="Здравствуйте!",
                status=MessageStatus.sent,
                external_message_id="222",
                author_user_id=15,
            )
        )
        await s.commit()
        ids = {"manager": 1, "account": 7, "contact": 10, "dialog": 20, "deal": 42}

    from app.db import get_session
    from app.web.app import create_app
    from app.web.deps import get_current_manager

    app = create_app()

    async def _override_session():
        async with SessionLocal() as s:
            yield s

    async def _override_manager():
        # Возвращаем засеянного менеджера без проверки куки (тестовый shortcut).
        async with SessionLocal() as s:
            res = await s.execute(select(Manager).where(Manager.id == 1))
            return res.scalar_one()

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_current_manager] = _override_manager

    client = TestClient(app)
    yield client, ids, SessionLocal
    app.dependency_overrides.clear()
    await engine.dispose()


def test_list_dialogs_returns_managers_dialogs(app_with_data):
    client, _ids, _ = app_with_data
    r = client.get("/api/dialogs")
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 1
    dlg = data[0]
    assert dlg["id"] == 20
    assert dlg["external_chat_id"] == "100200"
    assert dlg["contact_name"] == "Клиент"
    assert dlg["crm_deal_id"] == 42


def test_list_dialogs_filter_by_deal_id(app_with_data):
    client, _ids, _ = app_with_data
    r = client.get("/api/dialogs", params={"deal_id": 42})
    assert r.status_code == 200
    assert len(r.json()) == 1
    # Non-matching deal id -> empty.
    r2 = client.get("/api/dialogs", params={"deal_id": 999})
    assert r2.status_code == 200
    assert r2.json() == []


async def test_list_dialogs_filter_by_entity_type(app_with_data):
    """id-пространства сделок и лидов независимы: ?deal_id= обязан
    дискриминироваться типом сущности (виджет лида не видит сделки и
    наоборот; legacy-строка без crm_entity_type считается сделкой)."""
    from app.models import Dialog, Messenger

    client, ids, SessionLocal = app_with_data
    async with SessionLocal() as s:
        s.add(
            Dialog(
                id=30,
                contact_id=ids["contact"],
                messenger=Messenger.tg,
                external_chat_id="100300",
                assigned_user_id=1,
                crm_deal_id=42,
                crm_entity_type="lead",
            )
        )
        await s.commit()

    deals = client.get("/api/dialogs", params={"deal_id": 42}).json()
    leads = client.get("/api/dialogs", params={"deal_id": 42, "entity_type": "lead"}).json()
    assert [d["id"] for d in deals] == [20]
    assert [d["id"] for d in leads] == [30]


async def _seed_extra_messages(session_local, dialog_id: int) -> list[int]:
    """Досеивает сообщения с id 3..5 в диалог (fixture уже создала 1 и 2)."""
    from app.models import Message, MessageDirection, MessageStatus

    async with session_local() as s:
        for i in (3, 4, 5):
            s.add(
                Message(
                    id=i,
                    dialog_id=dialog_id,
                    direction=MessageDirection.inbound,
                    text=f"msg-{i}",
                    status=MessageStatus.delivered,
                )
            )
        await s.commit()
    return [3, 4, 5]


def test_get_messages_returns_history(app_with_data):
    client, ids, _ = app_with_data
    r = client.get(f"/api/dialogs/{ids['dialog']}/messages")
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 2
    texts = {m["text"] for m in data}
    assert "Привет" in texts
    assert "Здравствуйте!" in texts
    # первичная загрузка — новейшие первыми (DESC), UI разворачивает сам
    assert data[0]["id"] > data[1]["id"]


@pytest.mark.asyncio
async def test_get_messages_primary_load_newest_first(app_with_data):
    """Без параметров: новейшие N в DESC (limit=2 из 5 -> ids [5, 4])."""
    client, ids, session_local = app_with_data
    await _seed_extra_messages(session_local, ids["dialog"])
    r = client.get(f"/api/dialogs/{ids['dialog']}/messages", params={"limit": 2})
    assert r.status_code == 200
    assert [m["id"] for m in r.json()] == [5, 4]


@pytest.mark.asyncio
async def test_get_messages_before_returns_older_page(app_with_data):
    """before=<id>: сообщения старее курсора, тоже DESC (новейшие из старых)."""
    client, ids, session_local = app_with_data
    await _seed_extra_messages(session_local, ids["dialog"])
    r = client.get(
        f"/api/dialogs/{ids['dialog']}/messages", params={"before": 3, "limit": 10}
    )
    assert r.status_code == 200
    assert [m["id"] for m in r.json()] == [2, 1]


@pytest.mark.asyncio
async def test_get_messages_since_still_ascending(app_with_data):
    """since (poll-режим): контракт виджета не меняется — ASC от курсора."""
    client, ids, session_local = app_with_data
    await _seed_extra_messages(session_local, ids["dialog"])
    r = client.get(f"/api/dialogs/{ids['dialog']}/messages", params={"since": 2})
    assert r.status_code == 200
    assert [m["id"] for m in r.json()] == [3, 4, 5]


def test_get_messages_since_returns_only_new(app_with_data):
    client, ids, _ = app_with_data
    # since=1 -> only messages with id > 1.
    r = client.get(f"/api/dialogs/{ids['dialog']}/messages", params={"since": 1})
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 1
    assert data[0]["id"] == 2


def test_get_messages_unknown_dialog_returns_404(app_with_data):
    client, _ids, _ = app_with_data
    r = client.get("/api/dialogs/99999/messages")
    assert r.status_code == 404


def test_send_message_empty_text_returns_422(app_with_data):
    client, ids, _ = app_with_data
    r = client.post(f"/api/dialogs/{ids['dialog']}/messages", json={"text": ""})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_send_message_creates_message_and_outbox(app_with_data):
    """POST создаёт Message(status=pending) и OutboxItem(status=queued)
    атомарно; возвращается DTO нового сообщения."""
    from app.models import Message, OutboxItem, OutboxStatus

    client, ids, SessionLocal = app_with_data
    r = client.post(
        f"/api/dialogs/{ids['dialog']}/messages",
        json={"text": "Тестовый ответ"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["text"] == "Тестовый ответ"
    assert body["direction"] == "out"
    assert body["status"] == "pending"

    # Verify DB: new Message + OutboxItem created in one transaction.
    async with SessionLocal() as s:
        msgs = (
            await s.execute(
                select(Message)
                .where(Message.dialog_id == ids["dialog"])
                .order_by(Message.id)
            )
        ).scalars().all()
        assert any(m.text == "Тестовый ответ" for m in msgs)
        new_msg_id = next(m.id for m in msgs if m.text == "Тестовый ответ")

        outbox = (await s.execute(select(OutboxItem))).scalars().all()
        assert len(outbox) == 1
        assert outbox[0].dialog_id == ids["dialog"]
        assert outbox[0].tg_account_id == ids["account"]
        assert outbox[0].text == "Тестовый ответ"
        assert outbox[0].status == OutboxStatus.queued
        # OutboxItem связан с Message — воркер сможет закрыть статус.
        assert outbox[0].message_id == new_msg_id
