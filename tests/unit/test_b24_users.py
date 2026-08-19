"""b24/users: парсинг user.get, пагинация, upsert справочника менеджеров."""

from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.b24.users import (
    B24User,
    fetch_b24_users,
    upsert_managers_from_b24,
)
from app.models import Base, Manager, ManagerRole, Messenger, TgAccount, TgAccountStatus


class FakeClient:
    def __init__(self, pages: list[list]):
        self._pages = list(pages)
        self.calls: list[dict | None] = []

    async def call(self, method, auth_token, params=None, **kw):
        assert method == "user.get"
        self.calls.append(params)
        return self._pages.pop(0) if self._pages else []


def _row(uid, name="Иван", last="Иванов", active=True):
    return {"ID": str(uid), "NAME": name, "LAST_NAME": last, "ACTIVE": active}


async def test_fetch_paginates_by_start():
    pages = [[_row(i) for i in range(1, 51)], [_row(51), _row(52), _row(53)]]
    client = FakeClient(pages)
    users = await fetch_b24_users(client, "tok")
    assert len(users) == 53
    assert [p["start"] for p in client.calls] == [0, 50]


async def test_fetch_fail_closed_on_bad_rows():
    client = FakeClient(
        [[{"NAME": "нет ID"}, {"ID": "abc"}, None, _row(7, name=None, last=None)]]
    )
    users = await fetch_b24_users(client, "tok")
    assert len(users) == 1
    assert users[0].name == "Сотрудник 7"


def test_parse_null_names_and_active_variants():
    from app.b24.users import _parse_user

    assert _parse_user(_row(1, name=None, last="Петров")).name == "Петров"
    assert _parse_user(_row(2, active="N")).is_active is False
    assert _parse_user(_row(3, active="true")).is_active is True
    assert _parse_user({"ID": 4}).is_active is True


async def _db_with_managers(*managers: Manager):
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        s.add_all(managers)
        await s.commit()
    return factory, engine


async def _fetch_managers(factory):
    async with factory() as s:
        return (
            (await s.execute(select(Manager).order_by(Manager.b24_user_id))).scalars().all()
        )


async def test_upsert_creates_updates_and_deactivates():
    factory, engine = await _db_with_managers(
        Manager(id=1, name="Старое Имя", b24_user_id=10),
        Manager(id=2, name="Уволенный", b24_user_id=20),
    )
    result = await upsert_managers_from_b24(
        factory,
        [B24User(10, "Новое Имя", True), B24User(30, "Новый", True)],
    )
    assert result == {
        "created": 1,
        "updated": 1,
        "deactivated": 1,  # b24_user_id=20 исчез из портала
        "warnings": [],
    }
    managers = {m.b24_user_id: m for m in await _fetch_managers(factory)}
    assert managers[10].name == "Новое Имя"
    assert managers[20].is_active is False
    assert managers[30].is_active is True and managers[30].role == ManagerRole.manager

    # Идемпотентность повторного прогона.
    again = await upsert_managers_from_b24(
        factory, [B24User(10, "Новое Имя", True), B24User(30, "Новый", True)]
    )
    assert (again["created"], again["updated"], again["deactivated"]) == (0, 0, 0)
    await engine.dispose()


async def test_upsert_guards_accounts_and_last_supervisor():
    factory, engine = await _db_with_managers(
        Manager(id=1, name="Админ", b24_user_id=1, role=ManagerRole.supervisor),
        Manager(id=2, name="Маша с аккаунтом", b24_user_id=2),
        Manager(id=3, name="Бывший сотрудник", b24_user_id=3),
    )
    async with factory() as s:
        s.add(
            TgAccount(
                messenger=Messenger.tg,
                phone="79001112233",
                status=TgAccountStatus.active,
                manager_id=2,
            )
        )
        await s.commit()

    # В B24 остались только админ (деактивированный там) и Маша.
    result = await upsert_managers_from_b24(
        factory, [B24User(1, "Админ", False), B24User(2, "Маша с аккаунтом", True)]
    )
    assert result["deactivated"] == 1  # №3 исчез из портала, аккаунтов нет
    # Админ (ACTIVE=false в B24) не деактивирован — последний активный supervisor.
    assert len(result["warnings"]) == 1 and "последний активный администратор" in result["warnings"][0]
    managers = {m.b24_user_id: m for m in await _fetch_managers(factory)}
    assert managers[1].is_active is True and managers[1].role == ManagerRole.supervisor
    assert managers[2].is_active is True  # гард: активный аккаунт
    assert managers[3].is_active is False
    await engine.dispose()


async def test_upsert_b24_inactive_without_account_deactivates():
    factory, engine = await _db_with_managers(Manager(id=5, name="Оля", b24_user_id=50))
    result = await upsert_managers_from_b24(factory, [B24User(50, "Оля", False)])
    assert result["deactivated"] == 1
    assert (await _fetch_managers(factory))[0].is_active is False
    await engine.dispose()


# ---------------------------------------------------------------------- #
# Роут sync_b24 (прямые вызовы с подменёнными TokenManager/fetch)
# ---------------------------------------------------------------------- #
async def test_sync_route_upserts(monkeypatch):
    from app.web.routes import admin_api

    factory, engine = await _db_with_managers(
        Manager(id=1, name="Админ", b24_user_id=1, role=ManagerRole.supervisor)
    )
    monkeypatch.setattr(admin_api, "async_session", factory)

    class FakeTokenManager:
        def __init__(self, *, client_id, client_secret): ...

        async def get_token(self):
            return SimpleNamespace(access_token="tok", client_endpoint="")

    async def fake_fetch(client, auth_token):
        return [B24User(1, "Админ", True), B24User(9, "Из CRM", True)]

    monkeypatch.setattr(admin_api, "TokenManager", FakeTokenManager)
    monkeypatch.setattr(admin_api, "fetch_b24_users", fake_fetch)

    supervisor = Manager(id=1, name="Админ", b24_user_id=1, role=ManagerRole.supervisor)
    result = await admin_api.sync_managers_b24(supervisor)
    assert result["created"] == 1
    managers = {m.b24_user_id: m for m in await _fetch_managers(factory)}
    assert managers[9].name == "Из CRM"
    await engine.dispose()


async def test_sync_route_maps_scope_error(monkeypatch):
    import pytest
    from fastapi import HTTPException

    from app.b24.client import Bitrix24Error
    from app.web.routes import admin_api

    factory, engine = await _db_with_managers(
        Manager(id=1, name="Админ", b24_user_id=1, role=ManagerRole.supervisor)
    )
    monkeypatch.setattr(admin_api, "async_session", factory)

    class FakeTokenManager:
        def __init__(self, *, client_id, client_secret): ...

        async def get_token(self):
            return SimpleNamespace(access_token="tok", client_endpoint="")

    async def failing_fetch(client, auth_token):
        raise Bitrix24Error(code="ERROR_SCOPE", description="no scope")

    monkeypatch.setattr(admin_api, "TokenManager", FakeTokenManager)
    monkeypatch.setattr(admin_api, "fetch_b24_users", failing_fetch)

    supervisor = Manager(id=1, name="Админ", b24_user_id=1, role=ManagerRole.supervisor)
    with pytest.raises(HTTPException) as ei:
        await admin_api.sync_managers_b24(supervisor)
    assert ei.value.status_code == 400
    await engine.dispose()


# ---------------------------------------------------------------------- #
# PATCH role: гард последнего активного администратора
# ---------------------------------------------------------------------- #
async def test_patch_role_last_supervisor_guard(monkeypatch):
    import pytest
    from fastapi import HTTPException

    from app.web.routes import admin_api

    factory, engine = await _db_with_managers(
        Manager(id=1, name="Админ", b24_user_id=1, role=ManagerRole.supervisor),
        Manager(id=2, name="Маша", b24_user_id=2),
    )
    monkeypatch.setattr(admin_api, "async_session", factory)
    sup = Manager(id=1, name="Админ", b24_user_id=1, role=ManagerRole.supervisor)

    with pytest.raises(HTTPException) as ei:
        await admin_api.patch_manager(1, admin_api.ManagerPatchIn(role="manager"), sup)
    assert ei.value.status_code == 409

    # Повышение менеджера и понижение при втором админе — ок.
    await admin_api.patch_manager(2, admin_api.ManagerPatchIn(role="supervisor"), sup)
    demoted = await admin_api.patch_manager(
        1, admin_api.ManagerPatchIn(role="manager"), sup
    )
    assert demoted["role"] == "manager"
    await engine.dispose()
