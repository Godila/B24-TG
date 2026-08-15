from typing import ClassVar
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

    Базовое окружение выставляет conftest (_hermetic_env).
    """
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

    # OutboxWorker/CrmSyncWorker: реальный run() циклит вечно — подменяем.
    # HealthChecker: фейк-класс (план 009) — run() не циклит, заодно ловим
    # constructor-args (session_factory/notifier/admin_user_id).
    import app.bridge.crm_sync_worker as csw_mod
    import app.bridge.health_checker as hc_mod
    import app.bridge.outbox_worker as ow_mod
    monkeypatch.setattr(
        ow_mod.OutboxWorker, "run", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        csw_mod.CrmSyncWorker, "run", AsyncMock(return_value=None)
    )

    class FakeHealthChecker:
        init_kwargs: ClassVar[dict] = {}

        def __init__(self, sm, interval_sec=300, **kwargs):
            FakeHealthChecker.init_kwargs = {"sm": sm, "interval_sec": interval_sec, **kwargs}

        async def run(self) -> None:
            return None

        def stop(self) -> None:
            return None

    monkeypatch.setattr(hc_mod, "HealthChecker", FakeHealthChecker)

    await main_mod.run_bridge()

    # План 006: CRM — через очередь (crm_sync_enqueue), не напрямую b24sync.
    assert "crm_sync_enqueue" in constructed
    assert "db_session_factory" in constructed

    # План 009: HealthChecker подключён к БД и B24-алертам.
    hc_kwargs = FakeHealthChecker.init_kwargs
    assert hc_kwargs["session_factory"] is not None
    assert callable(hc_kwargs["notifier"])
    assert hc_kwargs["admin_user_id"] == 1  # default ALERT_ADMIN_B24_USER_ID
