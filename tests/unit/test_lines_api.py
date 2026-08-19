"""Этап 2 (линии): CRUD участников линий + инварианты account_members."""

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.models import (
    AccountMember,
    Base,
    Contact,
    Dialog,
    LineRole,
    Manager,
    ManagerRole,
    Messenger,
    TgAccount,
    TgAccountStatus,
)
from app.web.routes import admin_api


@pytest.fixture
async def db(monkeypatch):
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        s.add_all(
            [
                Manager(id=1, name="Админ", b24_user_id=1, role=ManagerRole.supervisor),
                Manager(id=2, name="Маша", b24_user_id=2),
                Manager(id=3, name="Оля неактивная", b24_user_id=3, is_active=False),
                Contact(id=50, messenger=Messenger.tg, external_user_id="c50"),
                TgAccount(
                    id=7,
                    messenger=Messenger.tg,
                    phone="79001112233",
                    status=TgAccountStatus.active,
                    manager_id=2,
                ),
                # То, что на проде делает backfill миграции f4b1d9c2a7e6:
                # владелец аккаунта — участник линии.
                AccountMember(account_id=7, manager_id=2, role=LineRole.participant),
            ]
        )
        await s.commit()
    from app.onboarding.max_channel import MaxOnboardingChannel
    from app.onboarding.tg_channel import TgOnboardingChannel

    admin_api.register_channels(
        {
            Messenger.tg: TgOnboardingChannel(session_factory=factory),
            Messenger.max: MaxOnboardingChannel(session_factory=factory),
        }
    )
    monkeypatch.setattr(admin_api, "async_session", factory)
    yield factory
    await engine.dispose()


async def _sup(db) -> Manager:
    async with db() as s:
        return await s.get(Manager, 1)


async def test_list_lines_groups_members(db):
    await admin_api.add_line_member(
        7, admin_api.LineMemberIn(manager_id=1, role=LineRole.observer), await _sup(db)
    )
    lines = await admin_api.list_lines(await _sup(db))
    assert len(lines) == 1
    ln = lines[0]
    assert ln["id"] == 7 and ln["messenger"] == "tg" and ln["status"] == "active"
    assert [m["manager_id"] for m in ln["members"]] == [2, 1]


async def test_add_member_guards_and_duplicate(db):
    sup = await _sup(db)
    with pytest.raises(HTTPException) as ei:
        await admin_api.add_line_member(999, admin_api.LineMemberIn(manager_id=1), sup)
    assert ei.value.status_code == 404

    with pytest.raises(HTTPException) as ei:
        await admin_api.add_line_member(7, admin_api.LineMemberIn(manager_id=3), sup)
    assert ei.value.status_code == 422  # неактивный менеджер

    created = await admin_api.add_line_member(
        7, admin_api.LineMemberIn(manager_id=1), sup
    )
    assert created["role"] == "participant"

    with pytest.raises(HTTPException) as ei:
        await admin_api.add_line_member(7, admin_api.LineMemberIn(manager_id=1), sup)
    assert ei.value.status_code == 409


async def test_patch_and_remove_member(db):
    sup = await _sup(db)
    await admin_api.add_line_member(7, admin_api.LineMemberIn(manager_id=1), sup)
    patched = await admin_api.patch_line_member(
        7, 1, admin_api.LineMemberPatchIn(role="observer"), sup
    )
    assert patched["role"] == "observer"

    # Ответственный диалог линии сбрасывается при удалении участника.
    async with db() as s:
        s.add(
            Dialog(
                contact_id=50,
                messenger=Messenger.tg,
                external_chat_id="9001",
                account_id=7,
                assigned_user_id=1,
            )
        )
        await s.commit()

    resp = await admin_api.remove_line_member(7, 1, sup)
    assert resp == {"status": "removed"}
    async with db() as s:
        members = (
            (await s.execute(select(AccountMember).where(AccountMember.account_id == 7)))
            .scalars()
            .all()
        )
        dialog = (
            await s.execute(select(Dialog).where(Dialog.external_chat_id == "9001"))
        ).scalar_one()
    assert [m.manager_id for m in members] == [2]  # владелец остался
    assert dialog.assigned_user_id is None

    with pytest.raises(HTTPException) as ei:
        await admin_api.remove_line_member(7, 1, sup)
    assert ei.value.status_code == 404


async def test_onboarding_creates_member_row(db):
    """Self-service подключение (пока жив) сразу заводит участника линии."""
    m2 = None
    async with db() as s:
        m2 = await s.get(Manager, 2)
    await admin_api.onboarding_start(Messenger.tg, m2)
    async with db() as s:
        account = (
            (await s.execute(select(TgAccount).where(TgAccount.manager_id == 2))).scalars()
        ).one()
        member = (
            await s.execute(
                select(AccountMember).where(AccountMember.account_id == account.id)
            )
        ).scalar_one_or_none()
    assert member is not None and member.manager_id == 2
    assert member.role == LineRole.participant
