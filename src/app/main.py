"""Точка входа. Режим выбирается аргументом: web | bridge | auth."""

import asyncio
import sys


def main() -> None:
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
    """Запуск bridge-процесса: пул TG-сессий + интеграция Bitrix24.

    Конструирует полный wiring (TokenManager → Bitrix24Client → CrmService/ImService
    → Bitrix24Sync → IncomingHandler). Загрузка аккаунтов из БД и регистрация
    обработчиков событий провайдеров — Фаза 4.
    """
    from app.b24.client import Bitrix24Client
    from app.b24.crm import CrmService
    from app.b24.im import ImService
    from app.b24.sync import Bitrix24Sync
    from app.b24.token_manager import TokenManager
    from app.bridge.incoming_handler import IncomingHandler
    from app.bridge.session_manager import SessionManager
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

    print(
        "Bridge started: B24 wiring готов. "
        "Загрузка TG-аккаунтов и подписка на события — Фаза 4."
    )
    try:
        await asyncio.Event().wait()  # бежим вечно
    finally:
        await sm.close_all()
    _ = handler  # handler готов к использованию в Фазе 4


if __name__ == "__main__":
    main()
