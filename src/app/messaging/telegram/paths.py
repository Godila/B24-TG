"""Единая конвенция on-disk путей TG-сессий.

Контракт трёх компонент: логин (QR/CLI) ЗАПИСЫВАЕТ сессию, SessionManager
(bridge) её ЧИТАЕТ, LoginCommandWorker поднимает логин-клиента. Путь обязан
строиться одинаково везде — рассинхрон layout'а даст «аккаунт не
авторизован» после успешного QR-скана. Никогда не собирайте путь руками.
"""

from pathlib import Path


def account_session_dir(sessions_dir: str | Path, account_id: int) -> Path:
    """Каталог сессий аккаунта: ``<sessions_dir>/account_<id>``.

    Провайдер хранит внутри файл ``session`` (см. TelegramProvider.session_file).
    Per-account подпапка критична: один общий каталог — и менеджеры
    перезаписывают .session-файлы друг друга.
    """
    return Path(sessions_dir) / f"account_{account_id}"


def tg_session_path(sessions_dir: str | Path, account_id: int) -> Path:
    """Полный путь к .session-файлу аккаунта: ``<dir>/account_<id>/session``."""
    return account_session_dir(sessions_dir, account_id) / "session"
