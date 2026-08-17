"""Пагинация /api/inbox/dialogs: keyset-курсор, полнота секции неотвеченных,
серверные фильтры supervisor'а.

Отдельный сид (не test_inbox_api.py): нужно заметно больше limit=50 диалогов, чтобы
limit=50 резал список на страницы.
"""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

_BASE = datetime(2026, 8, 17, 9, 0, 0, tzinfo=UTC)


@pytest.fixture
async def pages_app():
    """60 отвеченных диалогов Ивана (100..159, старше→новее) + древний
    неотвеченный (200) + 2 отвеченных Ольги (300, 301) + неназначенный (400)."""
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
        for cid in (10, 11, 12):
            s.add(
                Contact(
                    id=cid,
                    messenger=Messenger.tg,
                    external_user_id=str(cid),
                    name=f"Клиент {cid}",
                )
            )

        msg_id = 0

        async def _add_dialog(dialog_id: int, contact_id: int, assigned: int | None, minute: int):
            nonlocal msg_id
            s.add(
                Dialog(
                    id=dialog_id,
                    contact_id=contact_id,
                    messenger=Messenger.tg,
                    external_chat_id=str(dialog_id),
                    assigned_user_id=assigned,
                    last_msg_at=_BASE + timedelta(minutes=minute),
                )
            )
            # in → out: диалог отвечен, есть что считать агрегатам.
            for direction, offset in (
                (MessageDirection.inbound, 0),
                (MessageDirection.outbound, 1),
            ):
                msg_id += 1
                s.add(
                    Message(
                        id=msg_id,
                        dialog_id=dialog_id,
                        direction=direction,
                        text=f"m{msg_id}",
                        status=MessageStatus.delivered,
                        created_at=_BASE + timedelta(minutes=minute, seconds=offset),
                    )
                )

        # 60 отвеченных Ивана: 100 — старейший, 159 — новейший.
        for i in range(60):
            await _add_dialog(100 + i, 10, 1, i)
        # Древний неотвеченный Ивана: только входящее, старее всего списка.
        s.add(
            Dialog(
                id=200,
                contact_id=10,
                messenger=Messenger.tg,
                external_chat_id="200",
                assigned_user_id=1,
                last_msg_at=_BASE - timedelta(minutes=100),
            )
        )
        msg_id += 1
        s.add(
            Message(
                id=msg_id,
                dialog_id=200,
                direction=MessageDirection.inbound,
                text="ждём",
                status=MessageStatus.delivered,
                created_at=_BASE - timedelta(minutes=100),
            )
        )
        await _add_dialog(300, 11, 2, 70)
        await _add_dialog(301, 11, 2, 71)
        # Неназначенный без сообщений.
        s.add(
            Dialog(
                id=400,
                contact_id=12,
                messenger=Messenger.tg,
                external_chat_id="400",
                assigned_user_id=None,
            )
        )
        # «Пустой» диалог Ивана (NULL last_msg_at): NULLS LAST — в самом
        # конце списка, за пределами первой страницы; курсор обязан его
        # доставать (регресс: NULL-предикат выпадал из всех страниц после
        # первой).
        s.add(
            Dialog(
                id=500,
                contact_id=10,
                messenger=Messenger.tg,
                external_chat_id="500",
                assigned_user_id=1,
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
    yield client, state
    app.dependency_overrides.clear()
    await engine.dispose()


def test_first_page_cuts_at_limit(pages_app):
    client, state = pages_app
    state["manager_id"] = 1
    page = client.get("/api/inbox/dialogs", params={"limit": 50}).json()
    assert page["has_more"] is True
    ids = [d["id"] for d in page["dialogs"]]
    assert len(ids) == 50
    # Свежесть DESC: новейший (159) сверху, страница доходит до 110.
    assert ids[0] == 159
    assert ids[-1] == 110


def test_keyset_cursor_returns_older_tail_without_dupes(pages_app):
    client, state = pages_app
    state["manager_id"] = 1
    page1 = client.get("/api/inbox/dialogs", params={"limit": 50}).json()
    page2 = client.get(
        "/api/inbox/dialogs", params={"limit": 50, "before": page1["dialogs"][-1]["id"]}
    ).json()
    ids2 = [d["id"] for d in page2["dialogs"]]
    assert page2["has_more"] is False
    # 109..100 по свежести + NULL-хвост («пустой» D500 — NULLS LAST).
    assert ids2 == list(range(109, 99, -1)) + [500]
    assert not set(ids2) & {d["id"] for d in page1["dialogs"]}


def test_unanswered_section_is_complete_beyond_page(pages_app):
    """Древний неотвеченный диалог виден ВСЕГДА, хотя он за пределами
    первой страницы отвечавших (линза DESIGN.md: возраст ожидания)."""
    client, state = pages_app
    state["manager_id"] = 1
    page = client.get("/api/inbox/dialogs", params={"limit": 50}).json()
    assert [d["id"] for d in page["unanswered"]] == [200]
    assert 200 not in [d["id"] for d in page["dialogs"]]


def test_supervisor_assigned_filters(pages_app):
    client, state = pages_app
    state["manager_id"] = 3
    by_olga = client.get("/api/inbox/dialogs", params={"assigned": 2}).json()
    assert [d["id"] for d in by_olga["dialogs"]] == [301, 300]
    unassigned = client.get("/api/inbox/dialogs", params={"assigned": -1}).json()
    assert [d["id"] for d in unassigned["dialogs"]] == [400]


def test_assigned_param_ignored_for_plain_manager(pages_app):
    """Менеджер и так видит только своё — assigned не расширяет скоуп."""
    client, state = pages_app
    state["manager_id"] = 1
    page = client.get("/api/inbox/dialogs", params={"assigned": 2, "limit": 100}).json()
    assert len(page["dialogs"]) == 61
    assert all(d["is_mine"] for d in page["dialogs"])


def test_invalid_cursor_400(pages_app):
    client, state = pages_app
    state["manager_id"] = 1
    assert client.get("/api/inbox/dialogs", params={"before": 99999}).status_code == 400
    # Якорь вне скоупа (чужой диалог Ольги) — тоже невалидный курсор:
    # голый GET по PK иначе давал бы оракул существования чужих диалогов.
    assert client.get("/api/inbox/dialogs", params={"before": 301}).status_code == 400
