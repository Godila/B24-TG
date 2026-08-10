"""Центральная конфигурация приложения (pydantic-settings v2).

Settings читаются из переменных окружения и (опционально) файла `.env`.
Обязательные поля не имеют значений по умолчанию — конструкция `Settings()`
без соответствующего окружения упадёт с ошибкой валидации.

To keep import-time safe (см. task description), модуль НЕ создаёт синглтон
во время импорта. Вместо этого:
- класс `Settings` можно импортировать где угодно без побочных эффектов;
- готовый синглтон для production-кода доступен через `get_settings()`
  (ленивый, кэшируется через `functools.lru_cache`).
"""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Telegram (MTProto)
    tg_api_id: int = Field(...)
    tg_api_hash: str = Field(...)
    tg_sessions_dir: str = Field("/data/tg_sessions")

    # Bitrix24 OAuth
    b24_portal: str = Field(...)
    b24_client_id: str = Field(...)
    b24_client_secret: str = Field(...)
    b24_oauth_redirect: str = Field("https://localhost/oauth/callback")
    b24_webhook_secret: str = Field("")

    # Throttling (защита от бана)
    throttle_init_max: int = Field(10)
    throttle_init_window: int = Field(180)        # сек
    throttle_init_min_interval: int = Field(5)    # сек между инициациями
    throttle_reply_max: int = Field(20)           # ответов в минуту

    # Инфра
    database_url: str = Field(...)
    redis_url: str = Field(...)
    sentry_dsn: str = Field("")

    # Outbox
    outbox_poll_interval: int = Field(2)          # сек
    outbox_max_attempts: int = Field(5)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Возвращает единственный экземпляр настроек (ленивый синглтон).

    Первое обращение читает окружение и валидирует поля; результат
    кэшируется. Тесты, которым нужны свои значения, могут конструировать
    `Settings()` напрямую — это не зависит от данного кэша.
    """
    return Settings()
