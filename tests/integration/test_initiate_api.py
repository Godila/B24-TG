"""/api/dialogs/initiate + /api/accounts: guard'ы, дефолты, contact-ветка списка."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool


@pytest.fixture
async def env():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        from app.models import Base

        await conn.run_sync(Base.metadata.create_all)
    SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    from app.models import AccountMember, LineRole, Manager, Messenger, TgAccount, TgAccountStatus

    async with SessionLocal() as s:
        s.add(Manager(id=1, name="Иван", b24_user_id=15, is_active=True))
        s.add(Manager(id=2, name="Пётр", b24_user_id=16, is_active=True))
        s.add(TgAccount(id=7, messenger=Messenger.tg, phone="+79990000001",
                        session_path="/tmp/a", manager_id=1, status=TgAccountStatus.active))
        s.add(TgAccount(id=8, messenger=Messenger.max, phone="MAX-8",
                        device_id="d8", token="t8", manager_id=1, status=TgAccountStatus.active))
        s.add(TgAccount(id=9, messenger=Messenger.tg, phone="+79990000009",
                        session_path="/tmp/c", display_name="Общая линия",
                        status=TgAccountStatus.active))
        s.add(AccountMember(account_id=9, manager_id=1, role=LineRole.participant))
        await s.commit()

    from typing import Annotated

    from fastapi import Depends

    from app.db import get_session
    from app.web.app import create_app
    from app.web.deps import get_current_manager

    app = create_app()

    async def _override_session():
        async with SessionLocal() as s:
            yield s

    def _manager_override(manager_id: int):
        # Как в проде: менеджер грузится В request-scoped сессии (мутации
        # default_outbound в роуте обязаны персиститься).
        async def _override(
            session: Annotated[AsyncSession, Depends(get_session)],
        ):
            from app.models import Manager

            return await session.get(Manager, manager_id)

        return _override

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_current_manager] = _manager_override(1)

    from fastapi.testclient import TestClient

    client = TestClient(app)
    yield client, SessionLocal, app, _manager_override
    app.dependency_overrides.clear()
    await engine.dispose()


def test_accounts_scoping_and_default_flag(env):
    client, _sl, app, _mgr = env
    data = client.get("/api/accounts").json()
    assert {a["id"] for a in data} == {7, 8, 9}
    assert all(a["is_default"] is False for a in data)

    # Дефолт канала — через remember_account при инициировании.
    r = client.post("/api/dialogs/initiate", json={
        "messenger": "tg", "entity_type": "deal", "entity_id": 1,
        "account_id": 7, "dest": "+79991234567", "text": "привет",
        "remember_account": True,
    })
    assert r.status_code == 202
    data = client.get("/api/accounts").json()
    assert next(a for a in data if a["id"] == 7)["is_default"] is True

    # Чужой менеджер не видит чужих личных/участных аккаунтов.
    from app.web.deps import get_current_manager

    app.dependency_overrides[get_current_manager] = _mgr(2)
    assert client.get("/api/accounts").json() == []


def test_initiate_happy_with_default_account(env):
    client, SessionLocal, _app, _mgr = env
    # remember_account задает приоритетный аккаунт канала.
    assert client.post("/api/dialogs/initiate", json={
        "messenger": "tg", "entity_type": "deal", "entity_id": 1,
        "account_id": 7, "dest": "+79991112222", "text": "привет",
        "remember_account": True,
    }).status_code == 202
    r = client.post("/api/dialogs/initiate", json={
        "messenger": "tg", "entity_type": "deal", "entity_id": 42,
        "dest": "+7 999 123-45-67", "text": "Здравствуйте!",
    })
    assert r.status_code == 202
    cmd_id = r.json()["id"]

    from app.models import Initiation, InitiationStatus, Messenger

    async def check():
        async with SessionLocal() as s:
            cmd = await s.get(Initiation, cmd_id)
            assert cmd.account_id == 7  # приоритетный
            assert cmd.messenger is Messenger.tg
            assert cmd.dest_kind == "phone"
            assert cmd.dest_value == "+79991234567"  # нормализован
            assert cmd.status is InitiationStatus.pending

    import asyncio

    asyncio.run(check())

    status = client.get(f"/api/dialogs/initiate/{cmd_id}").json()
    assert status["status"] == "pending"


def test_initiate_requires_account_choice_when_several(env):
    """tg-аккаунтов два (личный + линия), дефолта нет → 409 «выберите»."""
    client, _sl, _app, _mgr = env
    r = client.post("/api/dialogs/initiate", json={
        "messenger": "tg", "entity_type": "deal", "entity_id": 1,
        "dest": "+79991234567", "text": "привет",
    })
    assert r.status_code == 409


def test_initiate_single_account_autopick(env):
    """max-аккаунт один → авто-выбор без дефолта."""
    client, _sl, _app, _mgr = env
    r = client.post("/api/dialogs/initiate", json={
        "messenger": "max", "entity_type": "contact", "entity_id": 3,
        "dest": "+79991234567", "text": "привет",
    })
    assert r.status_code == 202


def test_initiate_rejects_username_for_max(env):
    client, _sl, _app, _mgr = env
    r = client.post("/api/dialogs/initiate", json={
        "messenger": "max", "entity_type": "deal", "entity_id": 1,
        "dest": "@ivan_petrov", "text": "привет",
    })
    assert r.status_code == 422


def test_initiate_rejects_garbage_dest(env):
    client, _sl, _app, _mgr = env
    r = client.post("/api/dialogs/initiate", json={
        "messenger": "tg", "entity_type": "deal", "entity_id": 1,
        "dest": "12", "text": "привет",
    })
    assert r.status_code == 422


def test_initiate_duplicate_pending_is_409(env):
    client, _sl, _app, _mgr = env
    body = {"messenger": "max", "entity_type": "deal", "entity_id": 1,
            "dest": "+79991234567", "text": "привет"}
    assert client.post("/api/dialogs/initiate", json=body).status_code == 202
    r = client.post("/api/dialogs/initiate", json=body)
    assert r.status_code == 409


def test_initiate_readonly_forbidden(env):
    client, SessionLocal, _app, _mgr = env
    import asyncio

    from app.models import Manager

    async def set_readonly():
        async with SessionLocal() as s:
            (await s.get(Manager, 1)).is_readonly = True
            await s.commit()

    asyncio.run(set_readonly())
    r = client.post("/api/dialogs/initiate", json={
        "messenger": "max", "entity_type": "deal", "entity_id": 1,
        "dest": "+79991234567", "text": "привет",
    })
    assert r.status_code == 403


def test_initiate_foreign_account_404(env):
    client, _sl, _app, _mgr = env
    r = client.post("/api/dialogs/initiate", json={
        "messenger": "tg", "entity_type": "deal", "entity_id": 1,
        "account_id": 7, "dest": "+79991234567", "text": "привет",
    })
    assert r.status_code == 202  # свой — ок
    r2 = client.post("/api/dialogs/initiate", json={
        "messenger": "tg", "entity_type": "deal", "entity_id": 1,
        "account_id": 999, "dest": "+79991234567", "text": "привет",
    })
    assert r2.status_code == 404


def test_initiation_status_hidden_from_other_manager(env):
    client, _sl, app, _mgr = env
    cmd_id = client.post("/api/dialogs/initiate", json={
        "messenger": "max", "entity_type": "deal", "entity_id": 1,
        "dest": "+79991234567", "text": "привет",
    }).json()["id"]
    from app.web.deps import get_current_manager

    app.dependency_overrides[get_current_manager] = _mgr(2)
    assert client.get(f"/api/dialogs/initiate/{cmd_id}").status_code == 404


def test_remember_account_persists_default(env):
    client, SessionLocal, _app, _mgr = env
    r = client.post("/api/dialogs/initiate", json={
        "messenger": "tg", "entity_type": "deal", "entity_id": 1,
        "account_id": 9, "dest": "+79991234567", "text": "привет",
        "remember_account": True,
    })
    assert r.status_code == 202
    import asyncio

    from app.models import Manager

    async def check():
        async with SessionLocal() as s:
            assert (await s.get(Manager, 1)).default_outbound == {"tg": 9}

    asyncio.run(check())


def test_contact_card_lists_dialogs_via_contact_binding(env):
    """Контактная вкладка: диалоги через Contact.crm_contact_id (не через
    crm_entity_type — его у контактов нет by design)."""
    client, SessionLocal, _app, _mgr = env
    import asyncio

    from app.models import Contact, Dialog, Messenger

    async def seed():
        async with SessionLocal() as s:
            contact = Contact(messenger=Messenger.tg, external_user_id="55",
                              name="Клиент", crm_contact_id=321)
            s.add(contact)
            await s.flush()
            s.add(Dialog(contact_id=contact.id, messenger=Messenger.tg,
                         external_chat_id="55", account_id=7, assigned_user_id=1))
            await s.commit()

    asyncio.run(seed())
    data = client.get("/api/dialogs", params={"deal_id": 321, "entity_type": "contact"}).json()
    assert len(data) == 1
    assert data[0]["external_chat_id"] == "55"
    # Не-контактный id не показывает чужое.
    assert client.get("/api/dialogs", params={"deal_id": 999, "entity_type": "contact"}).json() == []


# --------------------------------------------------------------------- #
# Prefill «Кому»: телефон клиента из карточки
# --------------------------------------------------------------------- #
def test_prefill_uses_our_contact_phone_without_b24(env, monkeypatch):
    """Быстрый путь: контакт известен ЧатМосту — без похода в B24."""
    client, SessionLocal, _app, _mgr = env
    import asyncio

    from app.models import Contact, Messenger

    async def seed():
        async with SessionLocal() as s:
            s.add(Contact(messenger=Messenger.tg, external_user_id="77",
                          name="Клиент", phone="+79996543946", crm_contact_id=321))
            await s.commit()

    asyncio.run(seed())

    from unittest.mock import AsyncMock

    import app.web.routes.dialogs as dialogs_mod

    b24 = AsyncMock(return_value=None)
    monkeypatch.setattr(dialogs_mod, "_b24_entity_phone", b24)

    r = client.get("/api/dialogs/initiate/prefill",
                   params={"entity_type": "contact", "entity_id": 321})
    assert r.status_code == 200
    assert r.json() == {"phone": "+79996543946"}
    b24.assert_not_awaited()  # наш контакт — сети не было


def test_prefill_falls_back_to_b24(env, monkeypatch):
    """Нет в нашей БД (сделка) → B24; fail-open зашит внутрь хелпера."""
    client, _sl, _app, _mgr = env
    from unittest.mock import AsyncMock

    import app.web.routes.dialogs as dialogs_mod

    b24 = AsyncMock(return_value="+79991112233")
    monkeypatch.setattr(dialogs_mod, "_b24_entity_phone", b24)
    r = client.get("/api/dialogs/initiate/prefill",
                   params={"entity_type": "deal", "entity_id": 13})
    assert r.json() == {"phone": "+79991112233"}
    b24.assert_awaited_once_with("deal", 13)


def test_prefill_phone_extractor_formats():
    """Парсер PHONE из crm.*.get: список dict-ов, мусор → None (fail-closed)."""
    from app.web.routes.dialogs import _first_crm_phone

    assert _first_crm_phone({"PHONE": [{"ID": "1", "VALUE": " +7999 "}]}) == "+7999"
    assert _first_crm_phone({"PHONE": [{"VALUE": ""}, {"VALUE": "+79991112233"}]}) == "+79991112233"
    assert _first_crm_phone({"PHONE": []}) is None
    assert _first_crm_phone({}) is None
    assert _first_crm_phone(None) is None
    assert _first_crm_phone({"PHONE": "не-список"}) is None
