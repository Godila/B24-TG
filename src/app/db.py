"""Async SQLAlchemy engine + session factory.

Модуль создает движок и фабрику сессий на основе настроек из ``app.config``.
Импорт этого модуля в production запускает чтение конфигурации; в тестах без
переменных окружения ``get_settings()`` упадёт, поэтому тесты моделей
импортируют ``app.models`` напрямую, а не ``app.db``.
"""

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_settings

settings = get_settings()
engine = create_async_engine(settings.database_url, echo=False, pool_pre_ping=True)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI-зависимость: выдаёт async-сессию и закрывает её по выходу."""
    async with async_session() as session:
        yield session
