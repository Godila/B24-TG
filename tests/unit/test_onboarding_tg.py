"""TgOnboardingChannel: команды в БД (sqlite in-memory), без сети.

Ключ — линия (аккаунт): start принимает существующий аккаунт (заготовку
линии, созданную админом), а не менеджера."""

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
        # Заготовка линии (создаётся роутом POST /admin/api/lines).
        s.add(
            TgAccount(
                id=7, messenger=Messenger.tg, phone="TG-line",
                status=TgAccountStatus.offline,
            )
        )
        await s.commit()
    yield SessionLocal
    await engine.dispose()


async def _account(SessionLocal, account_id: int = 7) -> TgAccount:
    async with SessionLocal() as s:
        return await s.get(TgAccount, account_id)


async def _latest_cmd(SessionLocal) -> LoginCommand | None:
    async with SessionLocal() as s:
        return (
            await s.execute(
                select(LoginCommand).order_by(LoginCommand.id.desc()).limit(1)
            )
        ).scalar_one_or_none()


def _channel(SessionLocal) -> TgOnboardingChannel:
    return TgOnboardingChannel(session_factory=SessionLocal)


async def test_start_creates_command_for_line(db):
    resp = await _channel(db).start(await _account(db))
    assert resp["status"] == "waiting"

    cmd = await _latest_cmd(db)
    assert cmd.account_id == 7
    assert cmd.kind is LoginCommandKind.qr_login
    assert cmd.status is LoginCommandStatus.pending
    assert cmd.deadline_at is not None
    assert cmd.manager_id is None  # админская линия без legacy-владельца


async def test_start_active_account_without_force_already_active(db):
    async with db() as s:
        acc = await s.get(TgAccount, 7)
        acc.status = TgAccountStatus.active
        await s.commit()
    resp = await _channel(db).start(await _account(db))
    assert resp["status"] == "already_active"
    assert await _latest_cmd(db) is None


async def test_repeated_start_cancels_previous_command(db):
    ch = _channel(db)
    await ch.start(await _account(db))
    await ch.start(await _account(db))
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
    assert await ch.login_view(7) is None
    await ch.start(await _account(db))
    view = await ch.login_view(7)
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
    view = await ch.login_view(7)
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
    view = await ch.login_view(7)
    assert view.status.value == "error"
    # qr_link маскируется только в as_dict (контракт фронта)
    assert view.as_dict()["qr_link"] is None


async def test_submit_password_only_in_password_required(db):
    ch = _channel(db)
    await ch.start(await _account(db))
    assert await ch.submit_password(7, "x") is False
    async with db() as s:
        cmd = (
            await s.execute(
                select(LoginCommand).order_by(LoginCommand.id.desc()).limit(1)
            )
        ).scalar_one()
        cmd.status = LoginCommandStatus.password_required
        await s.commit()
    assert await ch.submit_password(7, "secret") is True
    async with db() as s:
        cmd = (
            await s.execute(
                select(LoginCommand).order_by(LoginCommand.id.desc()).limit(1)
            )
        ).scalar_one()
    assert cmd.password_transit == "secret"


async def test_cancel_terminalizes_and_wipes_password(db):
    ch = _channel(db)
    await ch.start(await _account(db))
    async with db() as s:
        cmd = (
            await s.execute(
                select(LoginCommand).order_by(LoginCommand.id.desc()).limit(1)
            )
        ).scalar_one()
        cmd.status = LoginCommandStatus.password_required
        cmd.password_transit = "pw"
        await s.commit()
    await ch.cancel(7)
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
    """uq_login_commands_active: вторая живая команда на ЛИНИЮ невозможна —
    старт обязан терминализировать прежнюю в той же txn."""
    ch = _channel(db)
    await ch.start(await _account(db))
    with pytest.raises(Exception):  # noqa: B017 - IntegrityError от partial index
        # прямая вставка в обход канала должна упасть по индексу
        async with db() as s:
            s.add(
                LoginCommand(
                    account_id=7,
                    messenger=Messenger.tg,
                    kind=LoginCommandKind.qr_login,
                )
            )
            await s.commit()
