from app.config import Settings, get_settings


def test_config_loads_from_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
    monkeypatch.setenv("TG_API_ID", "12345")
    monkeypatch.setenv("TG_API_HASH", "deadbeef")
    monkeypatch.setenv("TG_SESSIONS_DIR", "/tmp/sessions")
    monkeypatch.setenv("B24_PORTAL", "https://test.bitrix24.ru")
    monkeypatch.setenv("B24_CLIENT_ID", "cid")
    monkeypatch.setenv("B24_CLIENT_SECRET", "sec")
    monkeypatch.setenv("THROTTLE_INIT_MAX", "10")

    s = Settings()
    assert s.tg_api_id == 12345
    assert s.tg_api_hash == "deadbeef"
    assert s.throttle_init_max == 10
    assert s.b24_portal == "https://test.bitrix24.ru"


def test_get_settings_returns_cached_singleton(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
    monkeypatch.setenv("TG_API_ID", "12345")
    monkeypatch.setenv("TG_API_HASH", "deadbeef")
    monkeypatch.setenv("B24_PORTAL", "https://test.bitrix24.ru")
    monkeypatch.setenv("B24_CLIENT_ID", "cid")
    monkeypatch.setenv("B24_CLIENT_SECRET", "sec")

    get_settings.cache_clear()
    try:
        a = get_settings()
        b = get_settings()
        assert a is b
        assert isinstance(a, Settings)
    finally:
        get_settings.cache_clear()
