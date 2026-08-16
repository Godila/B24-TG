"""Контракт on-disk путей TG-сессий.

Логин (QR/CLI) пишет сессию, SessionManager читает, LoginCommandWorker
поднимает логин-клиента — все обязаны строить один и тот же путь. Тест
фиксирует layout и сверяет обе стороны контракта (helper ↔ провайдер).
"""

from pathlib import Path

from app.messaging.telegram.paths import account_session_dir, tg_session_path
from app.messaging.telegram.provider import TelegramProvider


def test_paths_layout(tmp_path: Path):
    assert tg_session_path(tmp_path, 7) == tmp_path / "account_7" / "session"
    assert account_session_dir(tmp_path, 7) == tmp_path / "account_7"
    # f-string без Path тоже нормализуется (login_worker передаёт str из настроек)
    assert tg_session_path(str(tmp_path), 7) == tmp_path / "account_7" / "session"


def test_provider_session_file_matches_convention(tmp_path: Path):
    """SessionManager передаёт провайдеру account_session_dir(); провайдер
    строит <dir>/session — сверка helper'а с фактическим путём провайдера."""
    provider = TelegramProvider(
        api_id=1, api_hash="x", sessions_dir=account_session_dir(tmp_path, 42)
    )
    assert provider.session_file == tg_session_path(tmp_path, 42)
