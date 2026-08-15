"""TgOnboardingChannel: команды в БД (sqlite in-memory), без сети."""


import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.models import (
    ACTIVE_STATUSES,
    Base,
    LoginCommand,
    LoginCommandKind,
    LoginCommandStatus,
    Manager,
    Messenger,
    TgAccount,
    TgAccountStatus,
)
from app.onboarding.tg_channel import TgOnboardingChannel


@pytest.fixture
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with SessionLocal() as s:
        s.add(Manager(id=1, name="Иван", b24_user_id=15, is_active=True))
        await s.commit()
    yield SessionLocal
    await engine.dispose()


async def _manager(SessionLocal) -> Manager:
    async with SessionLocal() as s:
        return (await s.execute(select(Manager).where(Manager.id == 1))).scalar_one()


async def _latest_cmd(SessionLocal) -> LoginCommand | None:
    async with SessionLocal() as s:
        return (
            await s.execute(
                select(LoginCommand).order_by(LoginCommand.id.desc()).limit(1)
            )
        ).scalar_one_or_none()


def _channel(SessionLocal) -> TgOnboardingChannel:
    return TgOnboardingChannel(session_factory=SessionLocal)


async def test_start_creates_account_placeholder_and_command(db):
    resp = await _channel(db).start(await _manager(db))
    assert resp["status"] == "waiting"

    async with db() as s:
        acc = (
            await s.execute(
                select(TgAccount).where(
                    TgAccount.manager_id == 1, TgAccount.messenger == Messenger.tg
                )
            )
        ).scalar_one()
    assert acc.phone == "TG-mgr1"  # placeholder, телефон не спрашиваем
    assert acc.status is TgAccountStatus.offline

    cmd = await _latest_cmd(db)
    assert cmd.kind is LoginCommandKind.qr_login
    assert cmd.status is LoginCommandStatus.pending
    assert cmd.deadline_at is not None


async def test_start_active_account_without_force_already_active(db):
    async with db() as s:
        s.add(
            TgAccount(
                messenger=Messenger.tg, phone="+79990000001",
                status=TgAccountStatus.active, manager_id=1,
            )
        )
        await s.commit()
    resp = await _channel(db).start(await _manager(db))
    assert resp["status"] == "already_active"
    assert await _latest_cmd(db) is None


async def test_repeated_start_cancels_previous_command(db):
    ch = _channel(db)
    await ch.start(await _manager(db))
    await ch.start(await _manager(db))
    cmd = await _latest_cmd(db)
    assert cmd.status is LoginCommandStatus.pending
    async with db() as s:
        old = (
            await s.execute(
                select(LoginCommand).where(LoginCommand.id == cmd.id - 1)
            )
        ).scalar_one()
    assert old.status is LoginCommandStatus.cancelled


async def test_login_view_maps_statuses(db):
    ch = _channel(db)
    assert await ch.login_view(1) is None
    await ch.start(await _manager(db))
    view = await ch.login_view(1)
    assert view is not None and view.status.value == "waiting"

    async with db() as s:
        cmd = (
            await s.execute(
                select(LoginCommand).order_by(LoginCommand.id.desc()).limit(1)
            )
        ).scalar_one()
        cmd.status = LoginCommandStatus.waiting
        cmd.qr_link = "tg://login?token=abc"
        await s.commit()
    view = await ch.login_view(1)
    assert view.qr_link == "tg://login?token=abc"

    # qr_link скрыт вне waiting
    async with db() as s:
        cmd = (
            await s.execute(
                select(LoginCommand).order_by(LoginCommand.id.desc()).limit(1)
            )
        ).scalar_one()
        cmd.status = LoginCommandStatus.error
        cmd.error = "boom"
        await s.commit()
    view = await ch.login_view(1)
    assert view.status.value == "error"
    # qr_link маскируется только в as_dict (контракт фронта)
    assert view.as_dict()["qr_link"] is None


async def test_submit_password_only_in_password_required(db):
    ch = _channel(db)
    await ch.start(await _manager(db))
    assert await ch.submit_password(1, "x") is False
    async with db() as s:
        cmd = (
            await s.execute(
                select(LoginCommand).order_by(LoginCommand.id.desc()).limit(1)
            )
        ).scalar_one()
        cmd.status = LoginCommandStatus.password_required
        await s.commit()
    assert await ch.submit_password(1, "secret") is True
    async with db() as s:
        cmd = (
            await s.execute(
                select(LoginCommand).order_by(LoginCommand.id.desc()).limit(1)
            )
        ).scalar_one()
    assert cmd.password_transit == "secret"


async def test_cancel_terminalizes_and_wipes_password(db):
    ch = _channel(db)
    await ch.start(await _manager(db))
    async with db() as s:
        cmd = (
            await s.execute(
                select(LoginCommand).order_by(LoginCommand.id.desc()).limit(1)
            )
        ).scalar_one()
        cmd.status = LoginCommandStatus.password_required
        cmd.password_transit = "pw"
        await s.commit()
    await ch.cancel(1)
    async with db() as s:
        rows = (
            (
                await s.execute(
                    select(LoginCommand).where(
                        LoginCommand.status.in_(ACTIVE_STATUSES)
                    )
                )
            )
            .scalars()
            .all()
        )
    assert rows == []
    cmd = await _latest_cmd(db)
    assert cmd.status is LoginCommandStatus.cancelled
    assert cmd.password_transit is None


async def test_partial_unique_blocks_second_active(db):
    """uq_login_commands_active: вторая живая команда на (manager, messenger)
    невозможна — старт обязан терминализировать прежнюю в той же txn."""
    ch = _channel(db)
    await ch.start(await _manager(db))
    with pytest.raises(Exception):  # noqa: B017 - IntegrityError от partial index
        # прямая вставка в обход канала должна упасть по индексу
        async with db() as s:
            acc = (
                await s.execute(
                    select(TgAccount).where(TgAccount.manager_id == 1)
                )
            ).scalar_one()
            s.add(
                LoginCommand(
                    manager_id=1,
                    account_id=acc.id,
                    messenger=Messenger.tg,
                    kind=LoginCommandKind.qr_login,
                )
            )
            await s.commit()
