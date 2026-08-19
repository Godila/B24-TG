"""verify_origin + admin_api: роуты панели и онбординга (прямые вызовы)."""

from types import SimpleNamespace

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
    Base,
    Manager,
    ManagerRole,
    Messenger,
    TgAccount,
    TgAccountStatus,
)
from app.web.deps import verify_origin
from app.web.routes import admin_api


def _request(method="POST", origin=None, host="b24-tg.haragy.top"):
    headers = {}
    if origin:
        headers["origin"] = origin
    if host:
        headers["host"] = host
    return SimpleNamespace(method=method, headers=headers)


def _settings_env(monkeypatch, **kw):
    monkeypatch.setattr(
        "app.config.get_settings",
        lambda: SimpleNamespace(b24_portal="b24-ye2jjz.bitrix24.ru", cors_origins="", **kw),
    )


def test_origin_absent_passes(monkeypatch):
    _settings_env(monkeypatch)
    verify_origin(_request(origin=None))  # same-origin/curl


def test_origin_same_host_passes(monkeypatch):
    _settings_env(monkeypatch)
    verify_origin(_request(origin="https://b24-tg.haragy.top"))


def test_origin_b24_portal_passes(monkeypatch):
    _settings_env(monkeypatch)
    verify_origin(_request(origin="https://b24-ye2jjz.bitrix24.ru"))


def test_origin_foreign_rejected(monkeypatch):
    _settings_env(monkeypatch)
    with pytest.raises(HTTPException) as ei:
        verify_origin(_request(origin="https://evil.example"))
    assert ei.value.status_code == 403


def test_origin_get_skipped(monkeypatch):
    _settings_env(monkeypatch)
    verify_origin(_request(method="GET", origin="https://evil.example"))


# ---------------------------------------------------------------------- #
# admin_api (прямые вызовы роутов; каналы — реальные, БД подменена)
# ---------------------------------------------------------------------- #
@pytest.fixture
async def db(monkeypatch):
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with SessionLocal() as s:
        s.add(
            Manager(
                id=1,
                name="Админ",
                b24_user_id=1,
                role=ManagerRole.supervisor,
                is_active=True,
            )
        )
        s.add(Manager(id=2, name="Маша", b24_user_id=2, is_active=True))
        s.add(
            TgAccount(
                id=7,
                messenger=Messenger.tg,
                phone="TG-mgr2",
                status=TgAccountStatus.offline,
                manager_id=2,
            )
        )
        await s.commit()
    monkeypatch.setattr(admin_api, "async_session", SessionLocal)
    # реальные каналы, но с подменённой фабрикой сессий
    from app.onboarding.max_channel import MaxOnboardingChannel
    from app.onboarding.tg_channel import TgOnboardingChannel

    admin_api.register_channels(
        {
            Messenger.tg: TgOnboardingChannel(session_factory=SessionLocal),
            Messenger.max: MaxOnboardingChannel(session_factory=SessionLocal),
        }
    )
    yield SessionLocal
    await engine.dispose()


async def _manager(db, mid) -> Manager:
    async with db() as s:
        return await s.get(Manager, mid)


async def test_me_returns_profile(db):
    me = await admin_api.me(await _manager(db, 2))
    assert me["role"] == "manager"
    assert me["is_readonly"] is False
    assert "accounts" not in me  # «Мои каналы» снесены — линии в своей секции


async def test_line_connect_issues_share_url(db):
    sup = await _manager(db, 1)
    resp = await admin_api.line_connect(7, Messenger.tg, sup)
    assert resp["status"] == "waiting"
    assert resp["share_url"].startswith("/connect/")
    # Канал обязан совпадать с линией.
    with pytest.raises(HTTPException) as ei:
        await admin_api.line_connect(7, Messenger.max, sup)
    assert ei.value.status_code == 409


async def test_supervisor_gate_rejects_manager_role(db):
    from app.web.deps import get_current_supervisor

    m = await _manager(db, 2)
    with pytest.raises(HTTPException) as ei:
        await get_current_supervisor(m)
    assert ei.value.status_code == 403


async def test_create_and_patch_manager(db):
    sup = await _manager(db, 1)
    created = await admin_api.create_manager(
        admin_api.ManagerCreateIn(name="Новый", b24_user_id=42), sup
    )
    assert created["b24_user_id"] == 42 and created["accounts"] == []

    patched = await admin_api.patch_manager(
        created["id"], admin_api.ManagerPatchIn(is_readonly=True), sup
    )
    assert patched["is_readonly"] is True

    with pytest.raises(HTTPException) as ei:
        await admin_api.create_manager(admin_api.ManagerCreateIn(name="Дубль", b24_user_id=42), sup)
    assert ei.value.status_code == 409


async def test_deactivate_manager_blocked_by_active_account(db):
    sup = await _manager(db, 1)
    async with db() as s:
        acc = await s.get(TgAccount, 7)
        acc.status = TgAccountStatus.active
        await s.commit()
    with pytest.raises(HTTPException) as ei:
        await admin_api.patch_manager(2, admin_api.ManagerPatchIn(is_active=False), sup)
    assert ei.value.status_code == 409
    assert "сначала отключите" in ei.value.detail


async def test_unlink_tg_schedules_logout(db):
    sup = await _manager(db, 1)
    resp = await admin_api.unlink_account(7, sup)
    assert resp["status"] == "logout_scheduled"
    async with db() as s:
        from app.models import LoginCommand, LoginCommandKind

        cmd = (
            await s.execute(select(LoginCommand).where(LoginCommand.account_id == 7))
        ).scalar_one()
    assert cmd.kind is LoginCommandKind.log_out


async def test_unlink_max_wipes_credentials(db):
    sup = await _manager(db, 1)
    async with db() as s:
        s.add(
            TgAccount(
                id=8,
                messenger=Messenger.max,
                phone="MAX-1",
                status=TgAccountStatus.active,
                manager_id=2,
                token="tok",
                device_id="dev",
            )
        )
        await s.commit()
    resp = await admin_api.unlink_account(8, sup)
    assert resp["status"] == "deactivated"
    async with db() as s:
        acc = await s.get(TgAccount, 8)
    assert acc.token is None and acc.status is TgAccountStatus.offline


# ---------------------------------------------------------------------- #
# Глобальные настройки (timeline_mode)
# ---------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_settings_default_is_first(db):
    supervisor = await _manager(db, 1)
    result = await admin_api.get_settings(supervisor)
    assert result == {"timeline_mode": "first", "media_to_timeline": False}


@pytest.mark.asyncio
async def test_settings_put_updates_mode(db):
    supervisor = await _manager(db, 1)
    await admin_api.put_settings(admin_api.SettingsIn(timeline_mode="all"), supervisor)
    assert (await admin_api.get_settings(supervisor))["timeline_mode"] == "all"
    # Перезапись другим значением — upsert, не дубль.
    await admin_api.put_settings(admin_api.SettingsIn(timeline_mode="none"), supervisor)
    assert (await admin_api.get_settings(supervisor))["timeline_mode"] == "none"


def test_settings_put_rejects_bad_mode():
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        admin_api.SettingsIn(timeline_mode="sometimes")


@pytest.mark.asyncio
async def test_settings_media_to_timeline_roundtrip(db):
    """Тумблер «файлы в комментариях CRM» читается/пишется независимо от
    timeline_mode (PUT только с media_to_timeline не трогает режим)."""
    supervisor = await _manager(db, 1)
    await admin_api.put_settings(admin_api.SettingsIn(media_to_timeline=True), supervisor)
    result = await admin_api.get_settings(supervisor)
    assert result["media_to_timeline"] is True
    assert result["timeline_mode"] == "first"  # режим не тронут

    await admin_api.put_settings(admin_api.SettingsIn(media_to_timeline=False), supervisor)
    assert (await admin_api.get_settings(supervisor))["media_to_timeline"] is False


async def test_unlink_removed_line_returns_404(db):
    """Удалённая (is_removed) линия не отключается — гард симметричен delete_line."""
    sup = await _manager(db, 1)
    async with db() as s:
        acc = await s.get(TgAccount, 7)
        acc.is_removed = True
        await s.commit()
    with pytest.raises(HTTPException) as ei:
        await admin_api.unlink_account(7, sup)
    assert ei.value.status_code == 404
