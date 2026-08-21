"""Точка входа. Режим выбирается аргументом: web | bridge | auth."""

import asyncio
import logging
import sys
from functools import partial

logger = logging.getLogger(__name__)


def main() -> None:
    # Настроим корневой логгер — без этого logger.info/error модулей
    # (bridge, b24, ...) никуда не выводятся. Формат с временем + уровнем.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    # httpx на INFO печатает полный URL каждого запроса — включая B24
    # OAuth (client_secret, refresh_token в query) и access_token в
    # REST-вызовах. Секретам не место в логах: глушим до WARNING
    # (ошибки сети/протокола остаются видны). httpcore — транспорт под
    # ним, страдает тем же.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
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
    """Запуск bridge-процесса: живой конвейер мессенджеры ↔ Bitrix24.

    Конструирует wiring (TokenManager → Bitrix24Client → CrmService/ImService
    → Bitrix24Sync → IncomingHandler + ReadMarker), затем активирует конвейер:
      1. загружает активные TgAccount (маршрутизация — по линиям, без менеджера);
      2. регистрирует их в SessionManager (подключение канальных сессий);
      3. запускает OutboxWorker, CrmSyncWorker и HealthChecker фоновыми
         задачами;
      4. для каждого подключённого аккаунта — pump (bootstrap.make_account_pump):
         одна таска = incoming_stream → IncomingHandler + read_receipt_stream
         → ReadMarker (✓✓).
    Останавливается по сигналу: отменяются все задачи, воркеры/health
    останавливаются, канальные сессии и общий B24 HTTP-клиент закрываются.
    """
    from app.b24.client import Bitrix24Client
    from app.b24.crm import CrmService
    from app.b24.im import ImService
    from app.b24.openlines import OpenLineService
    from app.b24.sync import Bitrix24Sync
    from app.b24.token_manager import TokenManager
    from app.bridge.account_sync import AccountSyncWorker, make_register_failure_hook
    from app.bridge.bootstrap import (
        load_active_accounts,
        make_account_pump,
        register_accounts,
    )
    from app.bridge.crm_sync_repo import WorkerCrmSyncRepository
    from app.bridge.crm_sync_worker import CrmSyncWorker
    from app.bridge.health_checker import HealthChecker
    from app.bridge.incoming_handler import IncomingHandler
    from app.bridge.login_worker import LoginCommandWorker
    from app.bridge.outbox_repo_worker import WorkerOutboxRepository
    from app.bridge.outbox_worker import OutboxWorker
    from app.bridge.read_marker import ReadMarker
    from app.bridge.session_manager import SessionManager
    from app.bridge.throttler import Throttler
    from app.config import get_settings
    from app.db import async_session
    from app.media.storage import MediaStorage
    from app.messaging.max.factory import build_max_provider
    from app.messaging.telegram.proxy import telethon_proxy
    from app.models import Messenger

    settings = get_settings()

    # Медиа-том: общий с web (upload менеджеров — раздача, сюда — скачивание
    # из каналов и отправка). Недоступный том ≠ смерть конвейера: сообщения
    # текут с плейсхолдерами, но это надо видеть в логах сразу.
    media_storage = MediaStorage(settings.media_dir, max_size_bytes=settings.media_max_size_bytes)
    if not media_storage.is_writable():
        logger.critical(
            "MEDIA STORAGE NOT WRITABLE: %s — входящие/исходящие медиа "
            "деградируют до плейсхолдеров",
            settings.media_dir,
        )

    sm = SessionManager(
        api_id=settings.tg_api_id,
        api_hash=settings.tg_api_hash,
        sessions_dir=settings.tg_sessions_dir,
        proxy=telethon_proxy(settings),
        # partial совместим с ProviderBuilder (Callable[[TgAccount], …]) —
        # протокол SessionManager не меняется, хранилище замыкается здесь.
        builders={Messenger.max: partial(build_max_provider, media_storage=media_storage)},
        register_timeout_sec=settings.register_timeout_sec,
        media_storage=media_storage,
        media_download_timeout_sec=settings.media_download_timeout_sec,
        media_send_timeout_sec=settings.media_send_timeout_sec,
    )

    # B24 wiring: ОДИН общий Bitrix24Client (shared TLS-коннекты, глобальный
    # throttle ~2 rps) на все сервисы; закрывается в finally при остановке.
    endpoint = settings.b24_portal.rstrip("/") + "/rest/"
    b24_client = Bitrix24Client(
        client_endpoint=endpoint,
        min_interval=settings.b24_min_call_interval,
    )
    crm = CrmService(b24_client)
    im = ImService(b24_client)
    token_mgr = TokenManager(
        client_id=settings.b24_client_id,
        client_secret=settings.b24_client_secret,
    )
    b24sync = Bitrix24Sync(token_mgr=token_mgr, crm=crm, im=im)
    # Открытые линии B24 (imconnector): входящие в чат линии, статусы доставки.
    openline = OpenLineService(token_mgr=token_mgr, client=b24_client)

    # Алерты админу (план 009): HealthChecker шлёт в B24-чат через TokenManager
    # + ImService на том же shared-клиенте, что и CRM-конвейер.
    async def admin_alert(user_id: int, text: str) -> None:
        token = await token_mgr.get_token()
        if token is None:
            logger.warning("Admin alert skipped: no B24 token (integration not installed)")
            return
        await im.notify_manager(token.access_token, user_id, text)

    # CRM-очередь (план 006): handler ставит задачи, воркер выполняет.
    crm_repo = WorkerCrmSyncRepository(async_session)

    async def enqueue_crm_sync(kind: str, message_id: int) -> None:
        await crm_repo.enqueue(kind=kind, message_id=message_id)

    handler = IncomingHandler(
        crm_sync_enqueue=enqueue_crm_sync,
        db_session_factory=async_session,
    )
    # Read-квитанции (✓✓): консьюмер + pump, объединяющий incoming и read
    # в одну forward-таску на аккаунт (см. bootstrap.make_account_pump).
    read_marker = ReadMarker(db_session_factory=async_session)
    account_pump = make_account_pump(read_marker)

    # 1-2. Аккаунты: загрузка + регистрация (подключение TG-сессий).
    accounts = await load_active_accounts(async_session)
    registered = await register_accounts(sm, accounts)

    # 4. Outbox-воркер: throttler per-account из настроек, репозиторий открывает
    #    свежую сессию на каждый вызов (см. WorkerOutboxRepository). После
    #    успешной отправки — crm_sync(kind=outbound) через on_sent_hook.
    def throttler_factory(_account_id: int) -> Throttler:
        return Throttler(
            reply_per_minute=settings.throttle_reply_max,
            init_max=settings.throttle_init_max,
            init_window_sec=settings.throttle_init_window,
            init_min_interval=settings.throttle_init_min_interval,
        )

    async def on_outbox_sent(message_id: int) -> None:
        await crm_repo.enqueue(kind="outbound", message_id=message_id)

    repo = WorkerOutboxRepository(async_session)
    worker = OutboxWorker(
        repo=repo,
        get_provider=sm.get,
        throttler_factory=throttler_factory,
        max_attempts=settings.outbox_max_attempts,
        poll_interval=settings.outbox_poll_interval,
        on_sent_hook=on_outbox_sent,
        media_storage=media_storage,
    )

    # CRM-воркер: те же retry-механики, что и outbox (5 попыток, backoff).
    # media_storage — чтение файлов вложений для FILES timeline-комментариев
    # (app_settings.media_to_timeline, выключено по умолчанию). openline —
    # ветка imconnector: сообщения OL-аккаунтов идут в чат линии, не в CRM.
    crm_worker = CrmSyncWorker(
        repo=crm_repo,
        b24sync=b24sync,
        max_attempts=settings.crm_sync_max_attempts,
        poll_interval=settings.crm_sync_poll_interval,
        media_storage=media_storage,
        media_timeline_max_bytes=settings.media_timeline_max_bytes,
        openline=openline,
    )

    # 5-6. Фоновые задачи: outbox + crm_sync + health + по задаче на входящий
    #      поток аккаунта + AccountSync (подхват новых active-аккаунтов после
    #      QR-онбординга без рестарта bridge). HealthChecker (план 009)
    #      персистит статусы сессий в tg_accounts.status (их читает /health
    #      в web-процессе) и алертит админа при отключении аккаунта.
    health = HealthChecker(
        sm,
        interval_sec=300,
        session_factory=async_session,
        notifier=admin_alert,
        admin_user_id=settings.alert_admin_b24_user_id,
    )
    account_sync = AccountSyncWorker(
        sm=sm,
        handler=handler,
        session_factory=async_session,
        forward=account_pump,
        interval_sec=settings.account_sync_interval_sec,
        on_register_failure=make_register_failure_hook(
            async_session, admin_alert, settings.alert_admin_b24_user_id
        ),
    )
    # TG QR-онбординг (вариант B): web пишет команды в login_commands,
    # bridge исполняет (единственный писатель .session-файлов).
    login_worker = LoginCommandWorker(
        sm=sm,
        account_sync=account_sync,
        session_factory=async_session,
        poll_interval=settings.login_worker_poll_sec,
        password_timeout_sec=settings.login_password_timeout_sec,
    )
    # «Написать первым»: web пишет команды в initiations, bridge резолвит
    # peer живым провайдером и создаёт диалог+сообщение в outbox.
    from app.bridge.initiation_worker import InitiationWorker

    initiation_worker = InitiationWorker(sm=sm, session_factory=async_session)
    tasks: list[asyncio.Task] = [
        asyncio.create_task(worker.run()),
        asyncio.create_task(crm_worker.run()),
        asyncio.create_task(health.run()),
        asyncio.create_task(account_sync.run()),
        asyncio.create_task(login_worker.run()),
        asyncio.create_task(initiation_worker.run()),
    ]
    for account in registered.values():
        provider = sm.get(account.id)
        if provider is not None:
            tasks.append(asyncio.create_task(account_pump(provider, account, handler)))

    logger.info(
        "Bridge started: %d account(s) registered, outbox+crm_sync+health+"
        "account_sync+incoming running",
        len(registered),
    )
    try:
        await asyncio.Event().wait()  # бежим вечно
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        worker.stop()
        crm_worker.stop()
        health.stop()
        account_sync.stop()
        login_worker.stop()
        initiation_worker.stop()
        await login_worker.shutdown()
        await account_sync.cancel_forwards()
        await sm.close_all()
        await b24_client.aclose()


if __name__ == "__main__":
    main()
