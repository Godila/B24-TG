from fastapi import FastAPI

from app.web.routes import health, webhook


def create_app() -> FastAPI:
    app = FastAPI(title="Bitrix-TG", version="0.1.0")
    app.include_router(health.router)
    app.include_router(webhook.router)
    return app
