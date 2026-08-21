"""Общий env-сетап для всех тестов: сьют не зависит от машины/локального .env.

Каждый тест, которому нужны ДРУГИЕ значения (DEV_MODE, CORS_ORIGINS),
переопределяет их сам через monkeypatch — autouse-фикстура ставит базу.
"""

import os
import tempfile
from pathlib import Path

import pytest

BASE_ENV = {
    "TG_API_ID": "1",
    "TG_API_HASH": "test",
    "TG_SESSIONS_DIR": "/tmp/tg_sessions",
    "B24_PORTAL": "https://test-portal.bitrix24.ru",
    "B24_CLIENT_ID": "test-client",
    "B24_CLIENT_SECRET": "test-secret",
    "DATABASE_URL": "sqlite+aiosqlite:///:memory:",
    "SESSION_SECRET": "test-session-secret",
    "DEV_MODE": "false",
    # Медиа-том: временный каталог (машина без /data); тесты вложений
    # переопределяют на свой tmp_path при необходимости.
    "MEDIA_DIR": str(Path(tempfile.mkdtemp(prefix="chatmost-media-"))),
}

# Выставляем базу ДО импорта тестовых модулей: некоторые из них
# (test_placement, test_health) импортируют app.web.app -> app.db,
# а app.db при импорте зовёт get_settings(). Без локального .env (CI)
# коллекция падала бы ещё до запуска фикстур. conftest импортируется
# pytest'ом раньше любых тестовых модулей — поэтому это безопасно.
for _key, _value in BASE_ENV.items():
    os.environ[_key] = _value


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch):
    for k, v in BASE_ENV.items():
        monkeypatch.setenv(k, v)
    from app.config import Settings, get_settings
    from app.media.storage import get_media_storage

    # env_file=".env" из CWD проглядывал мимо BASE_ENV (живой кейс 08-21:
    # PUBLIC_BASE_URL из смонтированного в прод-образ /repo/.env). Сьют
    # живёт только на env — файл настроек отключаем целиком.
    monkeypatch.setattr(
        Settings, "model_config", {**Settings.model_config, "env_file": None}
    )
    get_settings.cache_clear()
    # Синглтон storage держит путь из первого обращения — сбрасываем,
    # чтобы переопределённый MEDIA_DIR теста был услышан.
    get_media_storage.cache_clear()
    yield
    get_settings.cache_clear()
    get_media_storage.cache_clear()
