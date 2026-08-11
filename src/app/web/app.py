from fastapi import FastAPI

from app.web.routes import dialogs, health, placement, webhook


def create_app() -> FastAPI:
    app = FastAPI(title="Bitrix-TG", version="0.1.0")
    app.include_router(health.router)
    app.include_router(webhook.router)
    app.include_router(placement.router)
    app.include_router(dialogs.router)
    return app
