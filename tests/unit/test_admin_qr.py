"""Мок-тесты QR-спайка (план 010): TelegramClient подменён моком с
qr_login → {url, wait, recreate}; БД — in-memory SQLite (StaticPool).
Реальных вызовов Telethon нет. Живой эксперимент со сканом QR —
шаг оператора (см. docs/DESIGN-ADMIN-QR.md, «Живой эксперимент»).
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool
from telethon.errors import SessionPasswordNeededError

from app.models import Base, Manager, TgAccount, TgAccountStatus
from app.web.app import create_app
from app.web.routes import admin_qr

# --- Фикстуры ---------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_logins():
    """In-memory состояние логинов не должно протекать между тестами."""
    admin_qr._logins.clear()
    yield
    admin_qr._logins.clear()


@pytest.fixture
async def db(monkeypatch):
    """In-memory БД; фабрика сессий подменяет admin_qr.async_session."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(admin_qr, "async_session", factory)
    yield factory
    await engine.dispose()


@pytest.fixture
async def dev_env(monkeypatch, tmp_path):
    """DEV_MODE=true + sessions dir в tmp (не трогаем реальный /data)."""
    monkeypatch.setenv("DEV_MODE", "true")
    monkeypatch.setenv("TG_SESSIONS_DIR", str(tmp_path / "tg_sessions"))
    from app.config import get_settings

    get_settings.cache_clear()
    settings = get_settings()
    yield settings
    get_settings.cache_clear()


def _mock_qr(url="tg://login?token=abc", wait=None):
    qr = MagicMock()
    qr.url = url
    qr.wait = AsyncMock() if wait is None else AsyncMock(side_effect=wait)
    qr.recreate = AsyncMock()
    return qr


def _mock_client(qr, authorized=False):
    client = MagicMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.is_user_authorized = AsyncMock(return_value=authorized)
    client.qr_login = AsyncMock(return_value=qr)
    return client


def _patch_client(monkeypatch, client):
    monkeypatch.setattr(admin_qr, "TelegramClient", MagicMock(return_value=client))


# --- (a) start создаёт manager+account, если их нет -------------------


async def test_start_creates_manager_and_account(dev_env, db, monkeypatch):
    qr = _mock_qr()
    _patch_client(monkeypatch, _mock_client(qr))

    resp = await admin_qr.qr_start(b24_user_id=7, phone="+79990001122")

    assert resp["status"] == admin_qr.WAITING
    assert resp["qr_url"] == "tg://login?token=abc"
    acc_id = resp["account_id"]

    async with db() as s:
        mgr = (await s.execute(select(Manager))).scalar_one()
        acc = (await s.execute(select(TgAccount))).scalar_one()
        assert mgr.b24_user_id == 7
        assert acc.phone == "+79990001122"
        assert acc.manager_id == mgr.id
        # Path-контракт: per-account подпапка, как в auth.py / SessionManager.
        assert acc.session_path.endswith(f"account_{acc_id}/session")
        # Сессия ещё не активна — активирует только успешный скан.
        assert acc.status == TgAccountStatus.offline
    # Каталог сессии создан сразу (Telethon пишет туда .session).
    assert (Path(dev_env.tg_sessions_dir) / f"account_{acc_id}").is_dir()


async def test_start_updates_placeholder_phone_of_existing_account(dev_env, db, monkeypatch):
    """Заглушка из seed_manager (+70000000000) перезатирается реальным номером."""
    async with db() as s:
        mgr = Manager(name="Админ", b24_user_id=1, role="supervisor", is_active=True)
        s.add(mgr)
        await s.flush()
        s.add(
            TgAccount(
                phone="+70000000000", session_path="x", status=TgAccountStatus.offline,
                manager_id=mgr.id,
            )
        )
        await s.commit()

    _patch_client(monkeypatch, _mock_client(_mock_qr()))
    resp = await admin_qr.qr_start(b24_user_id=1, phone="+79995556677")

    async with db() as s:
        accounts = (await s.execute(select(TgAccount))).scalars().all()
        assert len(accounts) == 1
        assert accounts[0].id == resp["account_id"]
        assert accounts[0].phone == "+79995556677"


async def test_start_phone_rebind_to_taken_number_returns_409(dev_env, db, monkeypatch):
    """Смена номера существующего аккаунта на чужой занятый — 409, не 500.

    Регрессия: phone-update путь изначально пропускал проверку уникальности
    (она была только в ветке создания) → IntegrityError → 500.
    """
    async with db() as s:
        mgr1 = Manager(name="Один", b24_user_id=1, role="supervisor", is_active=True)
        mgr2 = Manager(name="Два", b24_user_id=2, role="manager", is_active=True)
        s.add_all([mgr1, mgr2])
        await s.flush()
        s.add_all([
            TgAccount(phone="+70000000000", session_path="x",
                      status=TgAccountStatus.offline, manager_id=mgr1.id),
            TgAccount(phone="+79995556677", session_path="y",
                      status=TgAccountStatus.offline, manager_id=mgr2.id),
        ])
        await s.commit()

    _patch_client(monkeypatch, _mock_client(_mock_qr()))
    with pytest.raises(HTTPException) as exc:
        await admin_qr.qr_start(b24_user_id=1, phone="+79995556677")
    assert exc.value.status_code == 409

    async with db() as s:
        accounts = (await s.execute(select(TgAccount))).scalars().all()
        assert {a.phone for a in accounts} == {"+70000000000", "+79995556677"}


async def test_start_phone_conflict_returns_409(dev_env, db, monkeypatch):
    async with db() as s:
        other = Manager(name="Другой", b24_user_id=1, is_active=True)
        s.add(other)
        await s.flush()
        s.add(
            TgAccount(
                phone="+79990001122", session_path="x",
                status=TgAccountStatus.offline, manager_id=other.id,
            )
        )
        await s.commit()

    _patch_client(monkeypatch, _mock_client(_mock_qr()))
    with pytest.raises(HTTPException) as exc:
        await admin_qr.qr_start(b24_user_id=2, phone="+79990001122")
    assert exc.value.status_code == 409


async def test_start_already_authorized_marks_active_immediately(dev_env, db, monkeypatch):
    """Повторный онбординг валидной сессии — без нового QR, сразу active."""
    client = _mock_client(_mock_qr(), authorized=True)
    _patch_client(monkeypatch, client)

    resp = await admin_qr.qr_start(b24_user_id=5, phone="+79991112233")

    assert resp["status"] == admin_qr.AUTHORIZED
    assert resp["qr_url"] is None
    client.qr_login.assert_not_awaited()
    async with db() as s:
        acc = (await s.execute(select(TgAccount))).scalar_one()
        assert acc.status == TgAccountStatus.active


# --- (b) успех wait() в фоне → status=active --------------------------


async def test_wait_success_sets_account_active(dev_env, db, monkeypatch):
    client = _mock_client(_mock_qr())
    _patch_client(monkeypatch, client)

    resp = await admin_qr.qr_start(b24_user_id=7, phone="+79990001122")
    state = admin_qr._logins[resp["account_id"]]
    assert state.status == admin_qr.WAITING

    # Детерминированно доводим фоновую корутину до конца.
    await state.task

    assert state.status == admin_qr.AUTHORIZED
    client.disconnect.assert_awaited()
    async with db() as s:
        acc = await s.get(TgAccount, resp["account_id"])
        assert acc.status == TgAccountStatus.active

    # /status уже отдаёт authorized и не показывает QR.
    status = await admin_qr.qr_status(account_id=resp["account_id"])
    assert status["status"] == admin_qr.AUTHORIZED
    assert status["qr_url"] is None


async def test_wait_timeout_recreates_qr_then_succeeds(dev_env, db, monkeypatch):
    """Таймаут первой итерации → recreate() → вторая успешна."""
    qr = _mock_qr(wait=[TimeoutError(), None])
    _patch_client(monkeypatch, _mock_client(qr))

    resp = await admin_qr.qr_start(b24_user_id=7, phone="+79990001122")
    await admin_qr._logins[resp["account_id"]].task

    qr.recreate.assert_awaited_once()
    assert admin_qr._logins[resp["account_id"]].status == admin_qr.AUTHORIZED


async def test_wait_timeout_all_attempts_expired(dev_env, db, monkeypatch):
    qr = _mock_qr(wait=TimeoutError)
    _patch_client(monkeypatch, _mock_client(qr))

    resp = await admin_qr.qr_start(b24_user_id=7, phone="+79990001122")
    await admin_qr._logins[resp["account_id"]].task

    # recreate только между итерациями: N-1 раз для N попыток.
    assert qr.recreate.await_count == admin_qr.MAX_QR_ITERATIONS - 1
    assert admin_qr._logins[resp["account_id"]].status == admin_qr.EXPIRED
    async with db() as s:
        acc = await s.get(TgAccount, resp["account_id"])
        assert acc.status == TgAccountStatus.offline


async def test_wait_2fa_password_reports_error(dev_env, db, monkeypatch):
    """2FA cloud-пароль: wait() кидает SessionPasswordNeededError (факт №7)."""
    qr = _mock_qr(wait=SessionPasswordNeededError(request=None))
    _patch_client(monkeypatch, _mock_client(qr))

    resp = await admin_qr.qr_start(b24_user_id=7, phone="+79990001122")
    await admin_qr._logins[resp["account_id"]].task

    state = admin_qr._logins[resp["account_id"]]
    assert state.status == admin_qr.ERROR
    assert "2FA" in state.error
    qr.recreate.assert_not_awaited()


async def test_status_unknown_account_returns_404(dev_env, db):
    with pytest.raises(HTTPException) as exc:
        await admin_qr.qr_status(account_id=999)
    assert exc.value.status_code == 404


# --- (c) вне dev_mode — 404 -------------------------------------------


async def test_not_dev_mode_start_returns_404(db, monkeypatch):
    monkeypatch.setenv("DEV_MODE", "false")
    from app.config import get_settings

    get_settings.cache_clear()
    with pytest.raises(HTTPException) as exc:
        await admin_qr.qr_start(b24_user_id=1, phone="+7999")
    assert exc.value.status_code == 404


def test_qr_page_served_in_dev(monkeypatch):
    monkeypatch.setenv("DEV_MODE", "true")
    from app.config import get_settings

    get_settings.cache_clear()
    client = TestClient(create_app())

    r = client.get("/dev/qr")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    # Страница ссылается на vendored QR-библиотеку и на наши эндпоинты.
    assert "/static/vendor/qrcode.min.js" in r.text
    assert "/dev/qr/start" in r.text
    assert "/dev/qr/status" in r.text


def test_qr_page_404_when_not_dev(monkeypatch):
    monkeypatch.setenv("DEV_MODE", "false")
    from app.config import get_settings

    get_settings.cache_clear()
    client = TestClient(create_app())
    assert client.get("/dev/qr").status_code == 404
    assert client.get("/dev/qr/start", params={"b24_user_id": 1, "phone": "+7"}).status_code == 404
    assert client.get("/dev/qr/status", params={"account_id": 1}).status_code == 404
