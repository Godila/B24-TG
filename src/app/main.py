"""Точка входа. Режим выбирается аргументом: web | bridge | auth."""

import asyncio
import logging
import sys

logger = logging.getLogger(__name__)


def main() -> None:
    # Настроим корневой логгер — без этого logger.info/error модулей
    # (bridge, b24, ...) никуда не выводятся. Формат с временем + уровнем.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    mode = sys.argv[1] if len(sys.argv) > 1 else "web"

    if mode == "web":
        import uvicorn

        from app.web.app import create_app

        app = create_app()
        uvicorn.run(app, host="0.0.0.0", port=8000)

    elif mode == "bridge":
        asyncio.run(run_bridge())

    elif mode == "auth":
        sys.argv = sys.argv[:1] + sys.argv[2:]  # снять 'auth'
        from app.messaging.telegram.auth import main as auth_main

        auth_main()

    else:
        print(f"Unknown mode: {mode}. Use: web | bridge | auth")
        sys.exit(1)


async def run_bridge() -> None:
    """Запуск bridge-процесса: живой конвейер TG ↔ Bitrix24.

    Конструирует wiring (TokenManager → Bitrix24Client → CrmService/ImService
    → Bitrix24Sync → IncomingHandler), затем активирует конвейер:
      1. загружает активные TgAccount (с eager-load менеджера);
      2. регистрирует их в SessionManager (подключение TG-сессий);
      3. запускает OutboxWorker и HealthChecker фоновыми задачами;
      4. для каждого подключённого аккаунта — подписку на incoming_stream
         с форвардом в IncomingHandler.
    Останавливается по сигналу: отменяются все задачи, воркер/health
    останавливаются, сессии закрываются.
    """
    from app.b24.client import Bitrix24Client
    from app.b24.crm import CrmService
    from app.b24.im import ImService
    from app.b24.sync import Bitrix24Sync
    from app.b24.token_manager import TokenManager
    from app.bridge.bootstrap import forward_incoming, load_active_accounts, register_accounts
    from app.bridge.health_checker import HealthChecker
    from app.bridge.incoming_handler import IncomingHandler
    from app.bridge.outbox_repo_worker import WorkerOutboxRepository
    from app.bridge.outbox_worker import OutboxWorker
    from app.bridge.session_manager import SessionManager
    from app.bridge.throttler import Throttler
    from app.config import get_settings
    from app.db import async_session

    settings = get_settings()

    sm = SessionManager(
        api_id=settings.tg_api_id,
        api_hash=settings.tg_api_hash,
        sessions_dir=settings.tg_sessions_dir,
    )

    # B24 wiring: общий client_endpoint портала (REST-методы доступны с этим URL).
    endpoint = settings.b24_portal.rstrip("/") + "/rest/"
    b24_client = Bitrix24Client(client_endpoint=endpoint)
    crm = CrmService(b24_client)
    im = ImService(Bitrix24Client(client_endpoint=endpoint))
    token_mgr = TokenManager(
        client_id=settings.b24_client_id,
        client_secret=settings.b24_client_secret,
    )
    b24sync = Bitrix24Sync(token_mgr=token_mgr, crm=crm, im=im)
    handler = IncomingHandler(
        session_mgr=sm,
        b24sync=b24sync,
        db_session_factory=async_session,
    )

    # 1-2. Аккаунты: загрузка + регистрация (подключение TG-сессий).
    accounts = await load_active_accounts(async_session)
    registered = await register_accounts(sm, accounts)

    # 4. Outbox-воркер: throttler per-account из настроек, репозиторий открывает
    #    свежую сессию на каждый вызов (см. WorkerOutboxRepository).
    def throttler_factory(_account_id: int) -> Throttler:
        return Throttler(
            reply_per_minute=settings.throttle_reply_max,
            init_max=settings.throttle_init_max,
            init_window_sec=settings.throttle_init_window,
            init_min_interval=settings.throttle_init_min_interval,
        )

    repo = WorkerOutboxRepository(async_session)
    worker = OutboxWorker(
        repo=repo,
        get_provider=sm.get,
        throttler_factory=throttler_factory,
        max_attempts=settings.outbox_max_attempts,
        poll_interval=settings.outbox_poll_interval,
    )

    # 5-6. Фоновые задачи: outbox + health + по задаче на входящий поток аккаунта.
    health = HealthChecker(sm)
    tasks: list[asyncio.Task] = [
        asyncio.create_task(worker.run()),
        asyncio.create_task(health.run()),
    ]
    for account in registered.values():
        provider = sm.get(account.id)
        if provider is not None:
            tasks.append(asyncio.create_task(forward_incoming(provider, account, handler)))

    logger.info(
        "Bridge started: %d account(s) registered, outbox+health+incoming running",
        len(registered),
    )
    try:
        await asyncio.Event().wait()  # бежим вечно
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        worker.stop()
        health.stop()
        await sm.close_all()


if __name__ == "__main__":
    main()
