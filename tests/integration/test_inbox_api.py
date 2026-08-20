"""Интеграционные тесты /api/inbox: список с агрегатами + гашение непрочитанных.

Фикстура сеет трёх менеджеров (менеджер1/менеджер2/supervisor) и диалоги с
разным чередованием направлений сообщений. ``state["manager_id"]`` — кто
«в системе» (override get_current_manager), тесты переключают между собой.
"""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

_BASE = datetime(2026, 8, 14, 10, 0, 0, tzinfo=UTC)


def _at(minute: int) -> datetime:
    return _BASE + timedelta(minutes=minute)


@pytest.fixture
async def inbox_app():
    """In-memory SQLite + сид + TestClient с переключаемым менеджером."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        from app.models import Base

        await conn.run_sync(Base.metadata.create_all)
    SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    from app.models import (
        Contact,
        Dialog,
        Manager,
        ManagerRole,
        Message,
        MessageDirection,
        MessageStatus,
        Messenger,
        TgAccount,
    )

    async with SessionLocal() as s:
        s.add(Manager(id=1, name="Иван", b24_user_id=15, is_active=True))
        s.add(Manager(id=2, name="Ольга", b24_user_id=16, is_active=True))
        s.add(
            Manager(
                id=3,
                name="Супервайзер",
                b24_user_id=17,
                is_active=True,
                role=ManagerRole.supervisor,
            )
        )
        s.add(
            TgAccount(
                id=7,
                messenger=Messenger.tg,
                phone="+79991234567",
                session_path="/tmp/s1",
                manager_id=1,
            )
        )
        s.add(
            TgAccount(
                id=8,
                messenger=Messenger.max,
                phone="+79991234568",
                session_path="/tmp/s2",
                manager_id=2,
            )
        )
        s.add(
            Contact(
                id=10,
                messenger=Messenger.tg,
                external_user_id="999",
                phone="+79990000001",
                name="Клиент TG",
            )
        )
        s.add(
            Contact(
                id=11,
                messenger=Messenger.max,
                external_user_id="888",
                phone="+79990000002",
                name="Клиент MAX",
            )
        )
        s.add(
            Contact(
                id=12,
                messenger=Messenger.max,
                external_user_id="777",
                phone="+79990000003",
                name="Тимофей",
            )
        )
        s.add(
            Contact(
                id=13, messenger=Messenger.tg, external_user_id="555", name="Без ответственного"
            )
        )

        # D20 (Иван, tg, сделка 42): in → out → in. Последний исходящий id=2,
        # курсор не открывали → неотвеченных 1 (id=3), непрочитанных 2 (1 и 3).
        s.add(
            Dialog(
                id=20,
                contact_id=10,
                messenger=Messenger.tg,
                external_chat_id="100200",
                assigned_user_id=1,
                crm_deal_id=42,
                last_msg_at=_at(3),
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
                created_at=_at(1),
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
                created_at=_at(2),
            )
        )
        s.add(
            Message(
                id=3,
                dialog_id=20,
                direction=MessageDirection.inbound,
                text="Ещё вопрос",
                status=MessageStatus.delivered,
                external_message_id="333",
                created_at=_at(3),
            )
        )

        # D21 (Ольга, max): только входящее → неотвеченных 1, непрочитанных 1.
        s.add(
            Dialog(
                id=21,
                contact_id=11,
                messenger=Messenger.max,
                external_chat_id="M-1",
                assigned_user_id=2,
                last_msg_at=_at(4),
            )
        )
        s.add(
            Message(
                id=4,
                dialog_id=21,
                direction=MessageDirection.inbound,
                text="Добрый день",
                status=MessageStatus.delivered,
                external_message_id="444",
                created_at=_at(4),
            )
        )

        # D22 (Иван, max): in → out → out → неотвеченных 0, непрочитанных 1.
        s.add(
            Dialog(
                id=22,
                contact_id=12,
                messenger=Messenger.max,
                external_chat_id="M-2",
                assigned_user_id=1,
                last_msg_at=_at(5),
            )
        )
        s.add(
            Message(
                id=5,
                dialog_id=22,
                direction=MessageDirection.inbound,
                text="Здравствуйте",
                status=MessageStatus.delivered,
                external_message_id="555",
                created_at=_at(5),
            )
        )
        s.add(
            Message(
                id=6,
                dialog_id=22,
                direction=MessageDirection.outbound,
                text="Привет",
                status=MessageStatus.sent,
                external_message_id="666",
                author_user_id=15,
                created_at=_at(5),
            )
        )
        s.add(
            Message(
                id=7,
                dialog_id=22,
                direction=MessageDirection.outbound,
                text="Держите",
                status=MessageStatus.sent,
                external_message_id="777",
                author_user_id=15,
                created_at=_at(5),
            )
        )

        # D23: неназначенный диалог без сообщений (виден только supervisor).
        s.add(
            Dialog(
                id=23,
                contact_id=13,
                messenger=Messenger.tg,
                external_chat_id="100300",
                assigned_user_id=None,
            )
        )
        await s.commit()

    from app.db import get_session
    from app.web.app import create_app
    from app.web.deps import get_current_manager

    app = create_app()
    state = {"manager_id": 1}

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
    yield client, state, SessionLocal
    app.dependency_overrides.clear()
    await engine.dispose()


def _all(page: dict) -> dict[int, dict]:
    """Обе секции ответа в один map по id (counters/поля едины)."""
    return {d["id"]: d for d in [*page["unanswered"], *page["dialogs"]]}


def test_manager_inbox_lists_only_own_dialogs(inbox_app):
    client, state, _ = inbox_app
    state["manager_id"] = 1
    r = client.get("/api/inbox/dialogs")
    assert r.status_code == 200
    page = r.json()
    # Только диалоги Ивана. D20 (входящее без ответа) — в секции
    # неотвеченных; «Диалоги» — отвечавшие по свежести (D22).
    assert [d["id"] for d in page["unanswered"]] == [20]
    assert [d["id"] for d in page["dialogs"]] == [22]
    assert page["has_more"] is False
    for d in [*page["unanswered"], *page["dialogs"]]:
        assert d["is_mine"] is True
        assert d["assigned_manager_name"] is None
    by_id = _all(page)
    assert by_id[22]["contact_name"] == "Тимофей"
    assert by_id[22]["unanswered_count"] == 0
    assert by_id[22]["unread_count"] == 1
    assert by_id[20]["unanswered_count"] == 1
    assert by_id[20]["unread_count"] == 2


def test_inbox_last_message_preview_and_deal_url(inbox_app):
    client, state, _ = inbox_app
    state["manager_id"] = 1
    by_id = _all(client.get("/api/inbox/dialogs").json())
    assert by_id[20]["last_message_direction"] == "in"
    assert by_id[20]["last_message_text"] == "Ещё вопрос"
    assert by_id[22]["last_message_direction"] == "out"
    assert by_id[22]["last_message_text"] == "Держите"
    assert by_id[20]["crm_deal_id"] == 42
    assert by_id[20]["deal_url"] == "https://test-portal.bitrix24.ru/crm/deal/42/view/"
    assert by_id[22]["deal_url"] is None


async def test_inbox_lead_dialog_gets_lead_url(inbox_app):
    """Диалог, перепривязанный к лиду: ссылка ведёт в карточку лида, тип —
    в DTO (фронт рисует метку «Лид» вместо «Сделка»)."""
    from app.models import Dialog

    client, state, SessionLocal = inbox_app
    state["manager_id"] = 1
    async with SessionLocal() as s:
        dlg = await s.get(Dialog, 20)
        dlg.crm_entity_type = "lead"
        await s.commit()

    by_id = _all(client.get("/api/inbox/dialogs").json())
    assert by_id[20]["crm_entity_type"] == "lead"
    assert by_id[20]["deal_url"] == "https://test-portal.bitrix24.ru/crm/lead/42/view/"


def test_supervisor_inbox_lists_all_with_owner_names(inbox_app):
    client, state, _ = inbox_app
    state["manager_id"] = 3
    r = client.get("/api/inbox/dialogs")
    assert r.status_code == 200
    page = r.json()
    # Неотвеченные: кто дольше ждёт — выше (D20 в 10:03 старее D21 в 10:04).
    # «Диалоги»: по свежести, NULLS LAST для диалога без сообщений (D23).
    assert [d["id"] for d in page["unanswered"]] == [20, 21]
    assert [d["id"] for d in page["dialogs"]] == [22, 23]
    by_id = _all(page)
    assert by_id[20]["assigned_manager_name"] == "Иван"
    assert by_id[21]["assigned_manager_name"] == "Ольга"
    assert by_id[23]["assigned_manager_name"] is None
    assert all(d["is_mine"] is False for d in _all(page).values())
    # Неназначенный диалог без сообщений: нулевые счётчики, нет превью.
    assert by_id[23]["unanswered_count"] == 0
    assert by_id[23]["unread_count"] == 0
    assert by_id[23]["last_message_direction"] is None


def test_inbox_messenger_filter(inbox_app):
    client, state, _ = inbox_app
    state["manager_id"] = 1
    r = client.get("/api/inbox/dialogs", params={"messenger": "tg"})
    assert r.status_code == 200
    page = r.json()
    # Единственный tg-диалог Ивана — неотвеченный D20.
    assert [d["id"] for d in page["unanswered"]] == [20]
    assert page["dialogs"] == []
    r2 = client.get("/api/inbox/dialogs", params={"messenger": "foo"})
    assert r2.status_code == 422


def test_read_marks_dialog_read_and_zeroes_unread(inbox_app):
    client, state, _ = inbox_app
    state["manager_id"] = 1
    r = client.post("/api/inbox/dialogs/20/read")
    assert r.status_code == 200
    assert r.json() == {"dialog_id": 20, "last_read_msg_id": 3}
    # Идемпотентно (повтор не ломает и не двигает курсор назад).
    r2 = client.post("/api/inbox/dialogs/20/read")
    assert r2.status_code == 200
    assert r2.json()["last_read_msg_id"] == 3
    # Непрочитанные погашены, неотвеченные остались (ответа всё ещё нет).
    by_id = _all(client.get("/api/inbox/dialogs").json())
    assert by_id[20]["unread_count"] == 0
    assert by_id[20]["unanswered_count"] == 1


def test_read_supervisor_own_cursor_owner_untouched(inbox_app):
    """Курсоры пер-менеджерные: supervisor гасит СВОЙ бейдж, курсор
    владельца (Ольги) не двигает."""
    client, state, _ = inbox_app
    state["manager_id"] = 3
    r = client.post("/api/inbox/dialogs/21/read")
    assert r.status_code == 200
    # Курсор Ольги не тронут: её непрочитанные на месте.
    state["manager_id"] = 2
    by_id = _all(client.get("/api/inbox/dialogs").json())
    assert by_id[21]["unread_count"] == 1


def test_read_foreign_dialog_404_for_manager(inbox_app):
    client, state, _ = inbox_app
    state["manager_id"] = 2
    r = client.post("/api/inbox/dialogs/20/read")
    assert r.status_code == 404


def test_read_unknown_dialog_404(inbox_app):
    client, state, _ = inbox_app
    state["manager_id"] = 1
    assert client.post("/api/inbox/dialogs/99999/read").status_code == 404


def test_supervisor_reads_foreign_history(inbox_app):
    client, state, _ = inbox_app
    state["manager_id"] = 3
    r = client.get("/api/dialogs/21/messages")
    assert r.status_code == 200
    assert [m["id"] for m in r.json()] == [4]


def test_manager_foreign_history_still_404(inbox_app):
    """Регресс: менеджер на чужом диалоге — 404, как и до «Чатов»."""
    client, state, _ = inbox_app
    state["manager_id"] = 1
    assert client.get("/api/dialogs/21/messages").status_code == 404


@pytest.mark.asyncio
async def test_supervisor_post_foreign_dialog_403_no_side_effects(inbox_app):
    client, state, session_local = inbox_app
    state["manager_id"] = 3
    r = client.post("/api/dialogs/21/messages", json={"text": "влез в чужой"})
    assert r.status_code == 403

    from app.models import Message, OutboxItem

    async with session_local() as s:
        msgs = (await s.execute(select(Message))).scalars().all()
        assert not any(m.text == "влез в чужой" for m in msgs)
        assert (await s.execute(select(OutboxItem))).scalars().first() is None


@pytest.mark.asyncio
async def test_owner_post_still_creates_message_and_outbox(inbox_app):
    """Регресс контракта виджета: владелец отправляет как раньше."""
    from app.models import Message, OutboxItem, OutboxStatus

    client, state, session_local = inbox_app
    state["manager_id"] = 2  # Ольга — владелец D21 (max), аккаунт id=8
    r = client.post("/api/dialogs/21/messages", json={"text": "Ответ Ольги"})
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "pending"

    async with session_local() as s:
        msg = (await s.execute(select(Message).where(Message.text == "Ответ Ольги"))).scalar_one()
        outbox = (await s.execute(select(OutboxItem))).scalars().one()
        assert outbox.message_id == msg.id
        assert outbox.tg_account_id == 8
        assert outbox.status == OutboxStatus.queued
