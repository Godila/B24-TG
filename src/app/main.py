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
        from app.bridge.session_manager import SessionManager
        from app.config import get_settings

        async def run_bridge() -> None:
            settings = get_settings()
            sm = SessionManager(
                api_id=settings.tg_api_id,
                api_hash=settings.tg_api_hash,
                sessions_dir=settings.tg_sessions_dir,
            )
            # В Фазе 1: просто держим процесс. Подгрузка аккаунтов — Фаза 2.
            print("Bridge started (Фаза 1: session loading в Фазе 2)")
            try:
                await asyncio.Event().wait()  # бежим вечно
            finally:
                await sm.close_all()

        asyncio.run(run_bridge())

    elif mode == "auth":
        sys.argv = sys.argv[:1] + sys.argv[2:]  # снять 'auth'
        from app.messaging.telegram.auth import main as auth_main

        auth_main()

    else:
        print(f"Unknown mode: {mode}. Use: web | bridge | auth")
        sys.exit(1)


if __name__ == "__main__":
    main()
