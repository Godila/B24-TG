"""Тесты telethon_proxy: сборка proxy-tuple для TelegramClient."""

import pytest

from app.config import Settings
from app.messaging.telegram.proxy import telethon_proxy


def _settings(**kw) -> Settings:
    base: dict = {
        "tg_api_id": 1,
        "tg_api_hash": "x",
        "b24_portal": "https://p.bitrix24.ru",
        "b24_client_id": "c",
        "b24_client_secret": "s",
        "database_url": "sqlite+aiosqlite:///:memory:",
        "session_secret": "sec",
    }
    base.update(kw)
    return Settings(**base)


def test_no_proxy_when_all_fields_empty():
    assert telethon_proxy(_settings()) is None


def test_socks5_full_tuple():
    proxy = telethon_proxy(_settings(
        tg_proxy_scheme="SOCKS5",
        tg_proxy_host="127.0.0.1",
        tg_proxy_port=1080,
        tg_proxy_username="u",
        tg_proxy_password="p",
    ))
    assert proxy == ("socks5", "127.0.0.1", 1080, True, "u", "p")


def test_http_without_auth_gives_none_credentials():
    proxy = telethon_proxy(_settings(
        tg_proxy_scheme="http",
        tg_proxy_host="proxy.local",
        tg_proxy_port=8080,
    ))
    assert proxy == ("http", "proxy.local", 8080, True, None, None)


def test_unknown_scheme_raises():
    with pytest.raises(ValueError, match="socks5"):
        telethon_proxy(_settings(tg_proxy_scheme="mtproxy", tg_proxy_host="h",
                                 tg_proxy_port=1))


def test_scheme_without_host_raises():
    with pytest.raises(ValueError, match="HOST"):
        telethon_proxy(_settings(tg_proxy_scheme="socks5"))
