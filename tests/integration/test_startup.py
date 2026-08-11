from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_run_bridge_wires_b24_components(monkeypatch):
    """run_bridge создаёт IncomingHandler и держит процесс через asyncio.Event.

    После Фазы 5 run_bridge также грузит аккаунты из БД и запускает фоновые
    задачи (outbox/health/incoming). Чтобы тест остался сфокусированным на
    wiring B24-компонентов и не зависел от реальной БД/сетевых циклов:
      - ``load_active_accounts`` патчится на ``[]`` (нет аккаунтов → нет
        регистрации и подписок на incoming);
      - ``OutboxWorker.run``/``HealthChecker.run`` — AsyncMock (не запускают
        реальный цикл);
      - ``asyncio.Event`` сразу set — run_bridge выходит из ``.wait()``.
    """
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("TG_API_ID", "1")
    monkeypatch.setenv("TG_API_HASH", "x")
    monkeypatch.setenv("TG_SESSIONS_DIR", "/tmp")
    monkeypatch.setenv("B24_PORTAL", "https://x.bitrix24.ru")
    monkeypatch.setenv("B24_CLIENT_ID", "c")
    monkeypatch.setenv("B24_CLIENT_SECRET", "s")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    # Сбрасываем кэш настроек, чтобы подхватились monkeypatch-окружение.
    from app.config import get_settings
    get_settings.cache_clear()

    import asyncio

    # Останавливаем вечный цикл сразу после запуска.
    real_event = asyncio.Event

    class StoppingEvent(real_event):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.set()  # сразу "прошёл" — run_bridge выйдет из .wait()

    import app.main as main_mod
    monkeypatch.setattr(main_mod.asyncio, "Event", StoppingEvent)

    # Перехватываем IncomingHandler, чтобы убедиться, что wiring дошел до него.
    constructed = {}

    class FakeHandler:
        def __init__(self, **kwargs):
            constructed.update(kwargs)

    monkeypatch.setattr("app.bridge.incoming_handler.IncomingHandler", FakeHandler)

    # Нет аккаунтов → нет регистрации/forwarding; БД не нужна.
    monkeypatch.setattr(
        "app.bridge.bootstrap.load_active_accounts",
        AsyncMock(return_value=[]),
    )

    # OutboxWorker/HealthChecker: реальный run() циклит вечно — подменяем.
    import app.bridge.health_checker as hc_mod
    import app.bridge.outbox_worker as ow_mod
    monkeypatch.setattr(
        ow_mod.OutboxWorker, "run", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        hc_mod.HealthChecker, "run", AsyncMock(return_value=None)
    )

    await main_mod.run_bridge()

    assert "b24sync" in constructed
    assert "session_mgr" in constructed
    assert "db_session_factory" in constructed
