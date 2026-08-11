import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "wiring-test-secret")
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("TG_API_ID", "1")
    monkeypatch.setenv("TG_API_HASH", "x")
    monkeypatch.setenv("B24_PORTAL", "https://x.bitrix24.ru")
    monkeypatch.setenv("B24_CLIENT_ID", "c")
    monkeypatch.setenv("B24_CLIENT_SECRET", "s")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("DEV_MODE", "true")
    monkeypatch.setenv("CORS_ORIGINS", "https://b24-x.bitrix24.ru,http://localhost:5173")
    from app.config import get_settings
    get_settings.cache_clear()
    from app.web.app import create_app
    return TestClient(create_app())


def test_cors_headers_on_api(client):
    r = client.get("/health", headers={"Origin": "https://b24-x.bitrix24.ru"})
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == "https://b24-x.bitrix24.ru"


def test_cors_preflight_options(client):
    r = client.options(
        "/api/dialogs",
        headers={
            "Origin": "https://b24-x.bitrix24.ru",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert r.status_code == 200
    assert "access-control-allow-origin" in {k.lower() for k in r.headers}


def test_static_files_served(client):
    # Static dir may not exist yet (Task 7 creates assets). The mount should exist;
    # requesting a non-existent file returns 404 (not 500/crash).
    r = client.get("/static/nonexistent.js")
    assert r.status_code == 404


def test_dev_login_sets_cookie_and_redirects(client):
    r = client.get(
        "/dev/login",
        params={"b24_user_id": "15", "deal_id": "42"},
        follow_redirects=False,
    )
    assert r.status_code in (302, 307)
    cookie_header = r.headers.get("set-cookie", "")
    assert "btg_sess=" in cookie_header
    # Redirects to the static chat page.
    assert "/static/" in r.headers.get("location", "") or "/placement" in r.headers.get("location", "")


def test_dev_login_disabled_in_prod(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "wiring-test-secret")
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("TG_API_ID", "1")
    monkeypatch.setenv("TG_API_HASH", "x")
    monkeypatch.setenv("B24_PORTAL", "https://x.bitrix24.ru")
    monkeypatch.setenv("B24_CLIENT_ID", "c")
    monkeypatch.setenv("B24_CLIENT_SECRET", "s")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("DEV_MODE", "false")
    from app.config import get_settings
    get_settings.cache_clear()
    from app.web.app import create_app
    client = TestClient(create_app())
    r = client.get("/dev/login", params={"b24_user_id": "15"}, follow_redirects=False)
    assert r.status_code == 404
