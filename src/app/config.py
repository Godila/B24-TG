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
    # Прокси для MTProto: часть РФ-хостингов блокирует подсети Telegram
    # (наш прод — как раз такой). Пустая схема = подключение напрямую.
    tg_proxy_scheme: str = Field("", description="socks5|socks4|http; пусто = без прокси")
    tg_proxy_host: str = Field("")
    tg_proxy_port: int = Field(0)
    tg_proxy_username: str = Field("")
    tg_proxy_password: str = Field("")

    # Bitrix24 OAuth
    b24_portal: str = Field(...)
    b24_client_id: str = Field(...)
    b24_client_secret: str = Field(...)
    # HMAC-секрет для проверки webhook ONAPPINSTALL (план 001).
    b24_webhook_secret: str = Field("")

    # Throttling (защита от бана)
    throttle_init_max: int = Field(10)
    throttle_init_window: int = Field(180)  # сек
    throttle_init_min_interval: int = Field(5)  # сек между инициациями
    throttle_reply_max: int = Field(20)  # ответов в минуту

    # Web / UI
    # HMAC-ключ для сессионных кук Web UI (длинная случайная строка).
    # Обязателен в production — без него приложение не запустится.
    session_secret: str = Field(...)
    # dev-режим: упрощённый auth без B24 (для локальной разработки).
    dev_mode: bool = Field(False)
    # Разрешённые CORS-origins (через запятую). Пусто = CORS отключён
    # (fail-closed: только same-origin). Для прод — домен портала B24.
    cors_origins: str = Field(
        "", description="CORS origins через запятую; пусто = CORS отключён (только same-origin)"
    )
    # Папка со статикой фронтенда (placement.html, app.js, style.css).
    static_dir: str = Field("src/app/static")

    # Медиа-вложения: общий docker-том web+bridge, в БД только метаданные.
    # Лимит = nginx client_max_body_size (26m с запасом на multipart-обёртку).
    media_dir: str = Field("/data/media")
    media_max_size_bytes: int = Field(25 * 1024 * 1024)
    # Таймаут скачивания входящего медиа: file_reference TG живёт минуты,
    # зависший download не должен держать очередь аккаунта.
    media_download_timeout_sec: float = Field(120.0)
    # Таймаут отправки: воркер обрабатывает outbox последовательно —
    # висящий upload 25МБ через полумёртвый туннель останавливает все
    # исходящие; лучше вернуться к элементу по расписанию backoff.
    media_send_timeout_sec: float = Field(300.0)
    # Лимит файла, загружаемого в timeline-комментарий B24 (FILES у
    # crm.timeline.comment.add): база64 раздувает payload вдвое, а диск
    # портала — не наш; большее остаётся текст-меткой в комментарии.
    media_timeline_max_bytes: int = Field(5 * 1024 * 1024)

    # Открытые линии B24 (imconnector): публичный адрес приложения, из него
    # строятся подписанные URL медиа (message.files) и handler-URL коннектора.
    # Пусто = файлы в линию не отправляются (текст работает), в лог WARNING.
    public_base_url: str = Field("")
    # TTL подписи публичной медиа-ссылки: B24 качает файл при приёме сообщения,
    # TTL закрывает повторные фетчи (превью) старых чатов линии.
    media_public_ttl_sec: int = Field(30 * 24 * 3600)
    # TTL подписи ссылки «Отвечать не нужно» в feed-уведомлении: кнопка живёт
    # в текущем сообщении, TTL должен пережить его до следующего рендера.
    notify_dismiss_ttl_sec: int = Field(30 * 24 * 3600)
    # Чат-бот «ЧатМост» (imbot.v2): 0 = бот не зарегистрирован — уведомления
    # идут от приложения (чат «Заметки»), кнопка гашения — LINK-фолбэк.
    imbot_bot_id: int = Field(0)

    # Активити бизнес-процессов (шаг «ЧатМост: отправить сообщение»):
    # таймаут скачивания [ссылки]-файла в хендлере. Бюджет меньше бюджета
    # ожидания очереди B24 — медленный файл должен получить честную ошибку
    # шага, а не оборванный соединением ответ после коммита.
    bizproc_download_timeout_sec: float = Field(20.0)

    # Инфра
    database_url: str = Field(...)

    # Outbox
    outbox_poll_interval: int = Field(2)  # сек
    outbox_max_attempts: int = Field(5)

    # Bitrix24 REST throttle (free-портал режет ~2 rps)
    b24_min_call_interval: float = Field(0.6)  # сек между вызовами

    # CRM sync queue (план 006)
    crm_sync_poll_interval: float = Field(2)  # сек
    crm_sync_max_attempts: int = Field(5)

    # Алерты о состоянии TG-сессий (план 009): b24_user_id админа, которому
    # HealthChecker шлёт уведомления в B24-чат (im.message.add).
    alert_admin_b24_user_id: int = Field(1)

    # MAX (эмуляция web-клиента; протокол ver=11 по wss). appVersion ДРЕЙФУЕТ
    # вслед за web-клиентом — симптом устаревания: qr_login.disabled на 288;
    # актуальную версию добывают из бандлов web.max.ru (рецепт в памяти
    # проекта project-max-channel).
    max_ws_url: str = Field("wss://ws-api.oneme.ru/websocket")
    max_origin: str = Field("https://web.max.ru")
    max_browser_ua: str = Field(
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )
    max_app_version: str = Field("26.8.4")
    max_request_timeout_sec: float = Field(15.0)
    # Heartbeat: сервер рвёт соединение после ~30-60с тишины; свой ping —
    # при простое >15с (серверный op=1 автоотвечается в ws_client).
    max_heartbeat_idle_sec: float = Field(15.0)
    max_heartbeat_tick_sec: float = Field(5.0)
    # Реконнект: ~30-50 LOGIN быстро сбрасывают токен → один провайдер держит
    # соединение и лечит его с backoff 2..32с.
    max_backoff_min_sec: float = Field(2.0)
    max_backoff_max_sec: float = Field(32.0)
    # QR-онбординг: сколько ждать скана всего и пароля 2FA отдельно.
    max_onboarding_deadline_sec: float = Field(300.0)
    max_onboarding_password_timeout_sec: float = Field(120.0)
    # Ожидание push op=136 «сервер обработал upload» после загрузки
    # файла/видео (по реверсу — до ~60с); выходит в общий бюджет
    # media_send_timeout_sec, невыполнение → send_timeout → ретрай outbox.
    max_upload_ready_timeout_sec: float = Field(60.0)

    # WhatsApp (OpenWA-сайдкар: REST + Socket.IO «/events», движок Baileys).
    # Пустой wa_api_key = канал не сконфигурирован (провайдер упадёт громко
    # при попытке регистрации WA-аккаунта, остальные каналы не задеты).
    wa_base_url: str = Field("http://openwa:2785")
    wa_api_key: str = Field("")
    # Egress WA-трафика сессии: WA блокирован в РФ-хостинге, OpenWA умеет
    # per-session proxyUrl (socks5) — гоняем через тот же vless+Reality
    # туннель, что и Telethon (xray-client).
    wa_proxy_url: str = Field("socks5://xray-client:10808")
    wa_request_timeout_sec: float = Field(20.0)
    # QR-онбординг: окно всей сессии подключения (создание + QR + скан).
    wa_onboarding_deadline_sec: float = Field(300.0)
    # Подхват новых active-аккаунтов bridge'ем (после QR-онбординга, без
    # рестарта) — период AccountSyncWorker.
    account_sync_interval_sec: float = Field(20.0)
    # Таймаут подключения аккаунта при регистрации. Telethon без таймаута
    # ждёт RPC бесконечно (мёртвый MTProto-прокси = вечное зависание,
    # блокирующее старт bridge целиком).
    register_timeout_sec: float = Field(60.0)

    # TG QR-онбординг (вариант B: команды в БД, bridge исполняет).
    tg_onboarding_deadline_sec: float = Field(900.0)  # окно всей команды
    login_password_timeout_sec: float = Field(120.0)  # ожидание 2FA-ввода
    login_worker_poll_sec: float = Field(2.0)  # каденс LoginCommandWorker
    # Share-ссылка подключения линии (/connect/<token>): TTL щедрее QR-окна —
    # ссылку ещё нужно переслать владельцу телефона.
    connect_token_ttl_sec: float = Field(1800.0)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Возвращает единственный экземпляр настроек (ленивый синглтон).

    Первое обращение читает окружение и валидирует поля; результат
    кэшируется. Тесты, которым нужны свои значения, могут конструировать
    `Settings()` напрямую — это не зависит от данного кэша.
    """
    return Settings()
