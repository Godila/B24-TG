"""Этап 3 (линии): видимость диалогов участникам + пер-менеджерные курсоры.

Общий номер = аккаунт-линия с несколькими участниками. Диалог линии виден
владельцу, каждому участнику (любой роли) и supervisor; посторонним — 404.
Непрочитанные — per-manager: гашение бейджа одним не трогает чужие.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import (
    AccountMember,
    Base,
    Contact,
    Dialog,
    LineRole,
    Manager,
    ManagerRole,
    Message,
    MessageDirection,
    MessageStatus,
    Messenger,
    TgAccount,
    TgAccountStatus,
)

OWNER, PARTICIPANT, OBSERVER, STRANGER, SUPERVISOR = 1, 2, 3, 4, 5


@pytest.fixture
async def lines_app():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with SessionLocal() as s:
        s.add_all(
            [
                Manager(id=OWNER, name="Владелец", b24_user_id=21),
                Manager(id=PARTICIPANT, name="Участник", b24_user_id=22),
                Manager(id=OBSERVER, name="Наблюдатель", b24_user_id=23),
                Manager(id=STRANGER, name="Посторонний", b24_user_id=24),
                Manager(
                    id=SUPERVISOR, name="Админ", b24_user_id=25, role=ManagerRole.supervisor
                ),
                Contact(id=30, messenger=Messenger.tg, external_user_id="777", name="Клиент"),
                # Линия-аккаунт: владелец + участник + наблюдатель.
                TgAccount(
                    id=9,
                    messenger=Messenger.tg,
                    phone="79005556677",
                    status=TgAccountStatus.active,
                    manager_id=OWNER,
                ),
                AccountMember(account_id=9, manager_id=OWNER),
                AccountMember(account_id=9, manager_id=PARTICIPANT),
                AccountMember(account_id=9, manager_id=OBSERVER, role=LineRole.observer),
                Dialog(
                    id=90,
                    contact_id=30,
                    messenger=Messenger.tg,
                    external_chat_id="700",
                    account_id=9,
                    assigned_user_id=OWNER,
                ),
                Message(
                    id=1,
                    dialog_id=90,
                    direction=MessageDirection.inbound,
                    text="Первое",
                    status=MessageStatus.delivered,
                    external_message_id="e1",
                ),
                Message(
                    id=2,
                    dialog_id=90,
                    direction=MessageDirection.inbound,
                    text="Второе",
                    status=MessageStatus.delivered,
                    external_message_id="e2",
                ),
            ]
        )
        await s.commit()

    from app.db import get_session
    from app.web.app import create_app
    from app.web.deps import get_current_manager

    app = create_app()
    state = {"manager_id": OWNER}

    async def _override_session():
        async with SessionLocal() as s:
            yield s

    async def _override_manager():
        async with SessionLocal() as s:
            res = await s.execute(select(Manager).where(Manager.id == state["manager_id"]))
            return res.scalar_one()

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_current_manager] = _override_manager
    client = TestClient(app)
    yield client, state
    app.dependency_overrides.clear()
    await engine.dispose()


def _inbox_ids(client) -> dict[int, dict]:
    page = client.get("/api/inbox/dialogs").json()
    return {d["id"]: d for d in [*page["unanswered"], *page["dialogs"]]}


def test_line_dialog_visible_to_members_only(lines_app):
    client, state = lines_app
    for mid, visible in (
        (OWNER, True),
        (PARTICIPANT, True),
        (OBSERVER, True),
        (SUPERVISOR, True),
        (STRANGER, False),
    ):
        state["manager_id"] = mid
        by_id = _inbox_ids(client)
        assert (90 in by_id) is visible, f"manager {mid}"
        if visible:
            assert by_id[90]["unread_count"] == 2


def test_line_dialog_history_access(lines_app):
    client, state = lines_app
    for mid, code in (
        (OWNER, 200),
        (PARTICIPANT, 200),
        (OBSERVER, 200),
        (SUPERVISOR, 200),
        (STRANGER, 404),
    ):
        state["manager_id"] = mid
        r = client.get("/api/dialogs/90/messages")
        assert r.status_code == code, f"manager {mid}"
        if code == 200:
            assert [m["id"] for m in r.json()] == [2, 1]


def test_deal_widget_list_includes_line_dialogs(lines_app):
    client, state = lines_app
    state["manager_id"] = PARTICIPANT
    ids = [d["id"] for d in client.get("/api/dialogs").json()]
    assert 90 in ids
    state["manager_id"] = STRANGER
    assert 90 not in [d["id"] for d in client.get("/api/dialogs").json()]


def test_read_cursors_are_per_manager(lines_app):
    client, state = lines_app
    # Участник открывает диалог — его бейдж гаснет, чужие нет.
    state["manager_id"] = PARTICIPANT
    assert client.post("/api/inbox/dialogs/90/read").status_code == 200
    assert _inbox_ids(client)[90]["unread_count"] == 0

    state["manager_id"] = OWNER
    assert _inbox_ids(client)[90]["unread_count"] == 2
    state["manager_id"] = OBSERVER
    assert _inbox_ids(client)[90]["unread_count"] == 2
    state["manager_id"] = SUPERVISOR
    assert _inbox_ids(client)[90]["unread_count"] == 2

    # Владелец гасит свой — у наблюдателя остаётся.
    state["manager_id"] = OWNER
    client.post("/api/inbox/dialogs/90/read")
    state["manager_id"] = OBSERVER
    assert _inbox_ids(client)[90]["unread_count"] == 2


def test_write_matrix_after_stage4(lines_app):
    """Этап 4: участник линии ПИШЕТ из общего номера (201), наблюдатель и
    supervisor-надзор — 403; посторонний — 404 (не существует для него)."""
    client, state = lines_app
    state["manager_id"] = PARTICIPANT
    r = client.post("/api/dialogs/90/messages", json={"text": "привет"})
    assert r.status_code == 201

    for mid, code in ((OBSERVER, 403), (SUPERVISOR, 403), (STRANGER, 404)):
        state["manager_id"] = mid
        r = client.post("/api/dialogs/90/messages", json={"text": "привет"})
        assert r.status_code == code, f"manager {mid}"
