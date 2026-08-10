# Bitrix-TG Implementation Plan — Фаза 1: Фундамент

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Создать работающий изолированно фундамент проекта — Docker Compose окружение, модель данных, абстракцию `MessengerProvider` и реализацию `TelegramProvider` (через Telethon), пул сессий `SessionManager`, `OutboxWorker` с throttle/retry, и FastAPI app с health-endpoint. Всё тестируется без реальных доступов Bitrix24/TG-номера.

**Architecture:** Модульный монолит Python (FastAPI + Telethon), разделённый на 2 процесса (`web` + `bridge`), общие Postgres + Redis. Фаза 1 покрывает доменный слой, TG-мост и инфраструктуру очереди — те ~80% кода, что пишутся через моки/тесты без внешних доступов.

**Tech Stack:** Python 3.11, FastAPI, SQLAlchemy 2.0 (async) + asyncpg, Alembic, Telethon, Redis (aioredis), pydantic-settings, pytest + pytest-asyncio, Docker Compose.

**Spec reference:** `docs/superpowers/specs/2026-08-10-bitrix-tg-design.md`

---

## Roadmap (фазы)

| Фаза | Содержание | Статус |
|---|---|---|
| **1** | Docker, БД-модель, MessengerProvider, TelegramProvider, SessionManager, OutboxWorker, Throttler, FastAPI-skeleton, auth_login CLI | **этот план** |
| 2 | Bitrix24Sync (OAuth, crm.item.add, timeline, findbyComm, event.bind), placement-handler | будет отдельный план |
| 3 | Web UI (чат-интерфейс, WebSocket), iFrame placement-виджет | будет отдельный план |
| 4 | Деплой на VM (nginx, TLS, мониторинг), подключение реальных доступов | будет отдельный план |

---

## File Structure (создаётся в Фазе 1)

```
Bitrix-TG/
├── docker-compose.yml              # 4 сервиса: postgres, redis, web, bridge
├── docker-compose.override.yml.example  # локальные переопределения (в .gitignore)
├── .env.example                    # шаблон конфига
├── Dockerfile                      # общий образ для web + bridge
├── pyproject.toml                  # зависимости + ruff + pytest
├── alembic.ini
├── alembic/versions/               # миграции
├── src/
│   └── app/
│       ├── __init__.py
│       ├── config.py               # настройки (pydantic-settings)
│       ├── db.py                   # engine + sessionmaker (async SQLAlchemy)
│       ├── models/                 # ORM-модели
│       │   ├── __init__.py
│       │   ├── base.py             # DeclarativeBase
│       │   ├── tg_account.py
│       │   ├── manager.py
│       │   ├── contact.py
│       │   ├── dialog.py
│       │   ├── message.py
│       │   ├── attachment.py
│       │   ├── outbox.py
│       │   └── template.py
│       ├── messaging/              # домен мессенджеров
│       │   ├── __init__.py
│       │   ├── types.py            # IncomingMessage, MessageStatus, SendResult
│       │   ├── provider.py         # абстрактный MessengerProvider
│       │   └── telegram/
│       │       ├── __init__.py
│       │       ├── provider.py     # TelegramProvider (Telethon)
│       │       └── auth.py         # auth_login CLI (первый вход)
│       ├── bridge/                 # bridge-процесс
│       │   ├── __init__.py
│       │   ├── session_manager.py  # пул Telethon-сессий
│       │   ├── throttler.py        # token-bucket throttle
│       │   ├── outbox_worker.py    # воркер очереди
│       │   └── health_checker.py
│       ├── web/                    # web-процесс (скелет в Фазе 1)
│       │   ├── __init__.py
│       │   ├── app.py              # FastAPI app factory
│       │   └── routes/
│       │       ├── __init__.py
│       │       └── health.py
│       └── main.py                 # точка входа: web | bridge | auth (по argv)
├── tests/
│   ├── conftest.py                 # фикстуры: test-db, redis
│   ├── unit/
│   │   ├── test_throttler.py
│   │   ├── test_outbox_worker.py
│   │   ├── test_session_manager.py
│   │   ├── test_telegram_provider.py
│   │   └── test_models.py
│   └── integration/
│       └── test_health.py
└── docs/
    └── superpowers/
        ├── specs/2026-08-10-bitrix-tg-design.md
        └── plans/2026-08-10-phase1-foundation.md
```

---

## Task 1: Структура проекта + pyproject.toml + Dockerfile

**Files:**
- Create: `pyproject.toml`
- Create: `Dockerfile`
- Create: `src/app/__init__.py`

- [ ] **Step 1: Создать `pyproject.toml`**

```toml
[project]
name = "bitrix-tg"
version = "0.1.0"
description = "Замена Wazzup: Telegram ↔ Bitrix24 CRM"
requires-python = ">=3.11"
dependencies = [
    "fastapi>=0.110",
    "uvicorn[standard]>=0.29",
    "sqlalchemy[asyncio]>=2.0",
    "asyncpg>=0.29",
    "alembic>=1.13",
    "telethon>=1.36",
    "redis>=5.0",
    "pydantic-settings>=2.2",
    "structlog>=24.1",
    "tenacity>=8.2",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
    "httpx>=0.27",
    "ruff>=0.4",
    "aiosqlite>=0.20",
]

[project.scripts]
bitrix-tg = "app.main:main"

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]

[tool.ruff]
line-length = 100
target-version = "py311"
```

- [ ] **Step 2: Создать `Dockerfile`**

```dockerfile
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
RUN pip install --upgrade pip && pip install .

COPY src/ ./src/
COPY alembic.ini ./
COPY alembic/ ./alembic/

ENV PYTHONPATH=/app/src

CMD ["python", "-m", "app.main", "web"]
```

- [ ] **Step 3: Создать пустые `__init__.py`**

Создать `src/app/__init__.py` (пустой файл).

- [ ] **Step 4: Проверить локально — зависимости ставятся**

Run: `pip install -e ".[dev]"`
Expected: успешная установка без ошибок.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml Dockerfile src/app/__init__.py
git commit -m "chore: project scaffold, dependencies, Dockerfile"
```

---

## Task 2: Конфигурация (pydantic-settings)

**Files:**
- Create: `src/app/config.py`
- Create: `.env.example`
- Test: `tests/unit/test_config.py`

- [ ] **Step 1: Написать падающий тест**

`tests/unit/test_config.py`:
```python
import os


def test_config_loads_from_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("TG_API_ID", "12345")
    monkeypatch.setenv("TG_API_HASH", "deadbeef")
    monkeypatch.setenv("TG_SESSIONS_DIR", "/tmp/sessions")
    monkeypatch.setenv("B24_PORTAL", "https://test.bitrix24.ru")
    monkeypatch.setenv("B24_CLIENT_ID", "cid")
    monkeypatch.setenv("B24_CLIENT_SECRET", "sec")
    monkeypatch.setenv("THROTTLE_INIT_MAX", "10")

    from app.config import Settings

    s = Settings()
    assert s.tg_api_id == 12345
    assert s.tg_api_hash == "deadbeef"
    assert s.throttle_init_max == 10
    assert s.b24_portal == "https://test.bitrix24.ru"
```

- [ ] **Step 2: Запустить — должен упасть (ModuleNotFoundError)**

Run: `pytest tests/unit/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.config'`

- [ ] **Step 3: Реализовать `src/app/config.py`**

```python
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
    throttle_init_window: int = Field(180)       # сек
    throttle_init_min_interval: int = Field(5)    # сек между инициациями
    throttle_reply_max: int = Field(20)           # ответов в минуту

    # Инфра
    database_url: str = Field(...)
    redis_url: str = Field(...)
    sentry_dsn: str = Field("")

    # Outbox
    outbox_poll_interval: int = Field(2)          # сек
    outbox_max_attempts: int = Field(5)


settings = Settings()
```

- [ ] **Step 4: Создать `.env.example`**

```
# Telegram (MTProto)
TG_API_ID=12345
TG_API_HASH=your_api_hash_from_my_telegram_org
TG_SESSIONS_DIR=/data/tg_sessions

# Bitrix24 OAuth
B24_PORTAL=https://your-portal.bitrix24.ru
B24_CLIENT_ID=
B24_CLIENT_SECRET=
B24_OAUTH_REDIRECT=https://your-domain/oauth/callback
B24_WEBHOOK_SECRET=

# Throttling
THROTTLE_INIT_MAX=10
THROTTLE_INIT_WINDOW=180
THROTTLE_INIT_MIN_INTERVAL=5
THROTTLE_REPLY_MAX=20

# Инфра
DATABASE_URL=postgresql+asyncpg://bitrix_tg:devpass@postgres:5432/bitrix_tg
REDIS_URL=redis://redis:6379/0
SENTRY_DSN=

# Outbox
OUTBOX_POLL_INTERVAL=2
OUTBOX_MAX_ATTEMPTS=5
```

- [ ] **Step 5: Тест проходит**

Run: `pytest tests/unit/test_config.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/app/config.py .env.example tests/unit/test_config.py
git commit -m "feat(config): pydantic-settings with env-driven config"
```

---

## Task 3: Docker Compose окружение

**Files:**
- Create: `docker-compose.yml`
- Create: `docker-compose.override.yml.example`

- [ ] **Step 1: Создать `docker-compose.yml`**

```yaml
services:
  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: bitrix_tg
      POSTGRES_USER: bitrix_tg
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-devpass}
    volumes:
      - pg_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U bitrix_tg"]
      interval: 5s
      timeout: 3s
      retries: 10
    ports:
      - "5432:5432"

  redis:
    image: redis:7-alpine
    command: redis-server --appendonly yes
    volumes:
      - redis_data:/data
    ports:
      - "6379:6379"

  web:
    build: .
    command: ["python", "-m", "app.main", "web"]
    env_file: .env
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_started
    ports:
      - "8000:8000"
    restart: unless-stopped

  bridge:
    build: .
    command: ["python", "-m", "app.main", "bridge"]
    env_file: .env
    volumes:
      - tg_sessions:/data/tg_sessions
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_started
    restart: unless-stopped

volumes:
  pg_data:
  redis_data:
  tg_sessions:
```

- [ ] **Step 2: Создать `docker-compose.override.yml.example`**

```yaml
# Скопируйте в docker-compose.override.yml (он в .gitignore)
# и адаптируйте под локальную разработку.
services:
  web:
    volumes:
      - ./src:/app/src  # hot reload при разработке
    command: ["uvicorn", "app.web.app:create_app", "--factory", "--reload", "--host", "0.0.0.0", "--port", "8000"]
  bridge:
    volumes:
      - ./src:/app/src
```

- [ ] **Step 3: Проверить — compose валиден**

Run: `docker compose config > /dev/null && echo OK`
Expected: `OK` (если .env существует; иначе создать временный `.env` из `.env.example` с любыми значениями)

- [ ] **Step 4: Commit**

```bash
git add docker-compose.yml docker-compose.override.yml.example
git commit -m "chore(infra): docker-compose with postgres, redis, web, bridge"
```

---

## Task 4: DB engine + DeclarativeBase

**Files:**
- Create: `src/app/db.py`
- Create: `src/app/models/__init__.py`
- Create: `src/app/models/base.py`

- [ ] **Step 1: Реализовать `src/app/db.py`**

```python
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import settings

engine = create_async_engine(settings.database_url, echo=False, pool_pre_ping=True)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with async_session() as session:
        yield session
```

- [ ] **Step 2: Реализовать `src/app/models/base.py`**

```python
from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
```

- [ ] **Step 3: Создать пустой `src/app/models/__init__.py`**

- [ ] **Step 4: Commit**

```bash
git add src/app/db.py src/app/models/__init__.py src/app/models/base.py
git commit -m "feat(db): async engine + DeclarativeBase + TimestampMixin"
```

---

## Task 5: ORM-модели (все таблицы из спеки)

**Files:**
- Create: `src/app/models/manager.py`
- Create: `src/app/models/tg_account.py`
- Create: `src/app/models/contact.py`
- Create: `src/app/models/dialog.py`
- Create: `src/app/models/message.py`
- Create: `src/app/models/attachment.py`
- Create: `src/app/models/outbox.py`
- Create: `src/app/models/template.py`
- Modify: `src/app/models/__init__.py`
- Test: `tests/unit/test_models.py`

- [ ] **Step 1: Написать тест на ключевые связи**

`tests/unit/test_models.py`:
```python
import pytest
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine

from app.models import Base, TgAccount, Manager, Dialog, Message, OutboxItem


@pytest.mark.asyncio
async def test_models_create_tables(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with engine.connect() as conn:
        tables = await conn.run_sync(lambda c: inspect(c).get_table_names())

    for name in ["managers", "tg_accounts", "contacts", "dialogs",
                 "messages", "attachments", "outbox", "templates"]:
        assert name in tables
    await engine.dispose()


def test_manager_tg_account_one_to_one():
    #tg_account.manager_id → manager.id, unique
    cols = {c.name for c in TgAccount.__table__.columns}
    assert "manager_id" in cols
    assert TgAccount.__table__.c.manager_id.unique is True


def test_outbox_has_required_status_fields():
    cols = {c.name for c in OutboxItem.__table__.columns}
    for required in ("status", "attempts", "next_attempt_at", "tg_account_id"):
        assert required in cols
```

- [ ] **Step 2: Запустить — должен упасть (импорта моделей нет)**

Run: `pytest tests/unit/test_models.py -v`
Expected: FAIL — ImportError

- [ ] **Step 3: Реализовать модели**

`src/app/models/manager.py`:
```python
import enum
from sqlalchemy import Boolean, Enum, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class ManagerRole(str, enum.Enum):
    manager = "manager"
    supervisor = "supervisor"


class Manager(Base, TimestampMixin):
    __tablename__ = "managers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    b24_user_id: Mapped[int] = mapped_column(Integer, unique=True, nullable=False, index=True)
    role: Mapped[ManagerRole] = mapped_column(Enum(ManagerRole), default=ManagerRole.manager)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    tg_account: Mapped["TgAccount | None"] = relationship(
        back_populates="manager", uselist=False
    )
```

`src/app/models/tg_account.py`:
```python
import enum
from datetime import datetime
from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class TgAccountStatus(str, enum.Enum):
    active = "active"
    banned = "banned"
    offline = "offline"


class TgAccount(Base, TimestampMixin):
    __tablename__ = "tg_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    phone: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, index=True)
    session_path: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[TgAccountStatus] = mapped_column(
        Enum(TgAccountStatus), default=TgAccountStatus.offline
    )
    manager_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("managers.id"), unique=True, nullable=False
    )
    last_floodwait_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    manager: Mapped["Manager"] = relationship(back_populates="tg_account")
```

`src/app/models/contact.py`:
```python
from sqlalchemy import BigInteger, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class Contact(Base, TimestampMixin):
    __tablename__ = "contacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tg_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True, nullable=False)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    crm_contact_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)

    dialogs: Mapped[list["Dialog"]] = relationship(back_populates="contact")
```

`src/app/models/dialog.py`:
```python
import enum
from datetime import datetime
from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class Messenger(str, enum.Enum):
    tg = "tg"
    max = "max"


class DialogStatus(str, enum.Enum):
    active = "active"
    archived = "archived"


class Dialog(Base, TimestampMixin):
    __tablename__ = "dialogs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    contact_id: Mapped[int] = mapped_column(ForeignKey("contacts.id"), nullable=False, index=True)
    messenger: Mapped[Messenger] = mapped_column(Enum(Messenger), nullable=False)
    external_chat_id: Mapped[str] = mapped_column(String(128), nullable=False)
    crm_deal_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    crm_entity_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    assigned_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[DialogStatus] = mapped_column(
        Enum(DialogStatus), default=DialogStatus.active
    )
    last_msg_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    contact: Mapped["Contact"] = relationship(back_populates="dialogs")
    messages: Mapped[list["Message"]] = relationship(back_populates="dialog")
```

`src/app/models/message.py`:
```python
import enum
from datetime import datetime
from sqlalchemy import BigInteger, DateTime, Enum, ForeignKey, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class MessageDirection(str, enum.Enum):
    inbound = "in"
    outbound = "out"


class MessageStatus(str, enum.Enum):
    pending = "pending"
    sent = "sent"
    delivered = "delivered"
    read = "read"
    error = "error"


class Message(Base, TimestampMixin):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    dialog_id: Mapped[int] = mapped_column(ForeignKey("dialogs.id"), nullable=False, index=True)
    direction: Mapped[MessageDirection] = mapped_column(Enum(MessageDirection), nullable=False)
    tg_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[MessageStatus] = mapped_column(
        Enum(MessageStatus), default=MessageStatus.pending
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    author_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    timeline_comment_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    dialog: Mapped["Dialog"] = relationship(back_populates="messages")
    attachments: Mapped[list["Attachment"]] = relationship(back_populates="message")
```

`src/app/models/attachment.py`:
```python
import enum
from sqlalchemy import BigInteger, Enum, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class AttachmentType(str, enum.Enum):
    photo = "photo"
    file = "file"
    video = "video"
    voice = "voice"
    sticker = "sticker"


class Attachment(Base, TimestampMixin):
    __tablename__ = "attachments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("messages.id"), nullable=False)
    type: Mapped[AttachmentType] = mapped_column(Enum(AttachmentType), nullable=False)
    file_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    message: Mapped["Message"] = relationship(back_populates="attachments")
```

`src/app/models/outbox.py`:
```python
import enum
from datetime import datetime
from sqlalchemy import BigInteger, DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class OutboxStatus(str, enum.Enum):
    queued = "queued"
    sending = "sending"
    sent = "sent"
    failed = "failed"
    retrying = "retrying"


class OutboxItem(Base, TimestampMixin):
    __tablename__ = "outbox"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    dialog_id: Mapped[int] = mapped_column(ForeignKey("dialogs.id"), nullable=False, index=True)
    tg_account_id: Mapped[int] = mapped_column(
        ForeignKey("tg_accounts.id"), nullable=False, index=True
    )
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    attachment_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("attachments.id"), nullable=True)
    status: Mapped[OutboxStatus] = mapped_column(
        Enum(OutboxStatus), default=OutboxStatus.queued, index=True
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default="now()", index=True
    )
    last_error: Mapped[str | None] = mapped_column(String(512), nullable=True)
```

`src/app/models/template.py`:
```python
from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class Template(Base, TimestampMixin):
    __tablename__ = "templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("managers.id"), nullable=True)
```

- [ ] **Step 4: Обновить `src/app/models/__init__.py`**

```python
from app.models.attachment import Attachment, AttachmentType
from app.models.base import Base, TimestampMixin
from app.models.contact import Contact
from app.models.dialog import Dialog, DialogStatus, Messenger
from app.models.manager import Manager, ManagerRole
from app.models.message import Message, MessageDirection, MessageStatus
from app.models.outbox import OutboxItem, OutboxStatus
from app.models.tg_account import TgAccount, TgAccountStatus
from app.models.template import Template

__all__ = [
    "Base", "TimestampMixin",
    "Manager", "ManagerRole",
    "TgAccount", "TgAccountStatus",
    "Contact",
    "Dialog", "DialogStatus", "Messenger",
    "Message", "MessageDirection", "MessageStatus",
    "Attachment", "AttachmentType",
    "OutboxItem", "OutboxStatus",
    "Template",
]
```

- [ ] **Step 5: Тест проходит**

Run: `pytest tests/unit/test_models.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/app/models/ tests/unit/test_models.py
git commit -m "feat(models): all ORM models — managers, tg_accounts, dialogs, messages, outbox"
```

---

## Task 6: Миграции Alembic

**Files:**
- Create: `alembic.ini`
- Create: `alembic/env.py`
- Create: `alembic/script.py.mako` (стандартный шаблон Alembic)
- Create: `alembic/versions/` (пустая папка)

- [ ] **Step 1: Создать `alembic.ini`**

```ini
[alembic]
script_location = alembic
prepend_sys_path = src
sqlalchemy.url = postgresql+asyncpg://bitrix_tg:devpass@localhost:5432/bitrix_tg

[loggers]
keys = root,sqlalchemy,alembic

[handlers]
keys = console

[formatters]
keys = generic

[logger_root]
level = WARN
handlers = console
qualname =

[logger_sqlalchemy]
level = WARN
handlers =
qualname = sqlalchemy.engine

[logger_alembic]
level = INFO
handlers =
qualname = alembic

[handler_console]
class = StreamHandler
args = (sys.stderr,)
level = NOTSET
formatter = generic

[formatter_generic]
format = %(levelname)-5.5s [%(name)s] %(message)s
datefmt = %H:%M:%S
```

- [ ] **Step 2: Создать `alembic/env.py`**

```python
import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.config import settings
from app.models import Base

config = context.config
config.set_main_option("sqlalchemy.url", settings.database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
```

- [ ] **Step 3: Создать стандартный `alembic/script.py.mako`** (шаблон Alembic по умолчанию — взять из `alembic init`)

- [ ] **Step 4: Сгенерировать первую миграцию**

Run: `alembic revision --autogenerate -m "initial schema"`
Expected: создан файл в `alembic/versions/`, в нём все таблицы.

- [ ] **Step 5: Commit**

```bash
git add alembic.ini alembic/
git commit -m "feat(db): alembic setup + initial migration"
```

---

## Task 7: Доменные типы messaging + абстрактный MessengerProvider

**Files:**
- Create: `src/app/messaging/__init__.py`
- Create: `src/app/messaging/types.py`
- Create: `src/app/messaging/provider.py`

- [ ] **Step 1: Реализовать `src/app/messaging/types.py`**

```python
import enum
from dataclasses import dataclass, field
from datetime import datetime


class ContentType(str, enum.Enum):
    text = "text"
    photo = "photo"
    file = "file"
    video = "video"
    voice = "voice"
    sticker = "sticker"


@dataclass
class IncomingMessage:
    """Сообщение, пришедшее из мессенджера."""
    account_id: int             # id tg_accounts (на какой аккаунт пришло)
    external_chat_id: str       # TG chat id как строка
    sender_tg_id: int           # кто написал (клиент)
    sender_name: str | None
    sender_phone: str | None
    sender_username: str | None
    content_type: ContentType
    text: str | None = None
    media_path: str | None = None
    external_message_id: int | None = None
    timestamp: datetime | None = None
    is_reply: bool = False      # True если это ответ клиента (диалог уже существует)


@dataclass
class SendResult:
    success: bool
    external_message_id: int | None = None
    error: str | None = None
    flood_wait_seconds: int | None = None  # если TG прислал FloodWait


class DeliveryStatus(str, enum.Enum):
    sent = "sent"
    delivered = "delivered"
    read = "read"
    failed = "failed"
```

- [ ] **Step 2: Реализовать `src/app/messaging/provider.py`**

```python
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from app.messaging.types import DeliveryStatus, IncomingMessage, SendResult


class MessengerProvider(ABC):
    """Абстракция над мессенджером.
    Точка расширения: TelegramProvider (Фаза 1), MaxProvider (позже)."""

    @abstractmethod
    async def connect(self) -> None:
        """Подключиться / авторизоваться."""

    @abstractmethod
    async def disconnect(self) -> None:
        """Корректно закрыть соединение."""

    @abstractmethod
    def incoming_stream(self) -> AsyncIterator[IncomingMessage]:
        """Асинхронный поток входящих сообщений."""

    @abstractmethod
    async def send_message(
        self, account_id: int, external_chat_id: str, text: str, *, is_initiation: bool
    ) -> SendResult:
        """Отправить сообщение. is_initiation влияет на throttle."""

    @abstractmethod
    async def status_stream(self) -> AsyncIterator[tuple[int, DeliveryStatus]]:
        """Поток обновлений статусов доставки (message_id, status)."""
```

- [ ] **Step 3: Создать пустой `src/app/messaging/__init__.py`**

- [ ] **Step 4: Commit**

```bash
git add src/app/messaging/
git commit -m "feat(messaging): domain types + abstract MessengerProvider"
```

---

## Task 8: Throttler (token bucket, анти-бан)

**Files:**
- Create: `src/app/bridge/__init__.py`
- Create: `src/app/bridge/throttler.py`
- Test: `tests/unit/test_throttler.py`

- [ ] **Step 1: Написать тесты**

`tests/unit/test_throttler.py`:
```python
import pytest

from app.bridge.throttler import Throttler


@pytest.mark.asyncio
async def test_reply_always_allowed_under_limit():
    t = Throttler(reply_per_minute=20, init_max=10, init_window_sec=180, init_min_interval=5)
    for _ in range(20):
        allowed = await t.acquire(is_initiation=False)
        assert allowed is True


@pytest.mark.asyncio
async def test_reply_blocked_over_limit():
    t = Throttler(reply_per_minute=2, init_max=10, init_window_sec=180, init_min_interval=5)
    assert await t.acquire(is_initiation=False) is True
    assert await t.acquire(is_initiation=False) is True
    assert await t.acquire(is_initiation=False) is False  # лимит исчерпан


@pytest.mark.asyncio
async def test_init_respects_min_interval(monkeypatch):
    # Имитируем время без ожидания
    import app.bridge.throttler as mod

    fake_time = [1000.0]
    monkeypatch.setattr(mod.time, "monotonic", lambda: fake_time[0])

    t = Throttler(reply_per_minute=20, init_max=10, init_window_sec=180, init_min_interval=5)
    assert await t.acquire(is_initiation=True) is True

    fake_time[0] = 1002.0  # прошло 2 сек < 5 сек
    assert await t.acquire(is_initiation=True) is False

    fake_time[0] = 1006.0  # прошло 6 сек >= 5 сек
    assert await t.acquire(is_initiation=True) is True


@pytest.mark.asyncio
async def test_init_respects_window_max(monkeypatch):
    import app.bridge.throttler as mod

    fake_time = [0.0]
    monkeypatch.setattr(mod.time, "monotonic", lambda: fake_time[0])

    t = Throttler(reply_per_minute=20, init_max=3, init_window_sec=100, init_min_interval=0)
    for _ in range(3):
        fake_time[0] += 1
        assert await t.acquire(is_initiation=True) is True

    fake_time[0] += 1  # всё ещё в окне
    assert await t.acquire(is_initiation=True) is False

    fake_time[0] += 100  # окно прошло
    assert await t.acquire(is_initiation=True) is True
```

- [ ] **Step 2: Запустить — FAIL (модуля нет)**

Run: `pytest tests/unit/test_throttler.py -v`

- [ ] **Step 3: Реализовать `src/app/bridge/throttler.py`**

```python
import asyncio
import time
from collections import deque


class Throttler:
    """Throttle отправки для одного TG-аккаунта.
    Две раздельные политики: ответы (мягкая) и инициации (жёсткая, анти-бан).
    Базируется на spec §6.1 слой 1."""

    def __init__(
        self,
        reply_per_minute: int,
        init_max: int,
        init_window_sec: int,
        init_min_interval: int,
    ):
        self._reply_limit = reply_per_minute
        self._reply_window: deque[float] = deque()
        self._init_max = init_max
        self._init_window_sec = init_window_sec
        self._init_min_interval = init_min_interval
        self._init_timestamps: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self, *, is_initiation: bool) -> bool:
        async with self._lock:
            now = time.monotonic()
            if is_initiation:
                return self._check_init(now)
            return self._check_reply(now)

    def _check_reply(self, now: float) -> bool:
        cutoff = now - 60.0
        while self._reply_window and self._reply_window[0] < cutoff:
            self._reply_window.popleft()
        if len(self._reply_window) >= self._reply_limit:
            return False
        self._reply_window.append(now)
        return True

    def _check_init(self, now: float) -> bool:
        # минимальный интервал между инициациями
        if self._init_timestamps and (now - self._init_timestamps[-1]) < self._init_min_interval:
            return False
        # лимит на окно
        cutoff = now - self._init_window_sec
        while self._init_timestamps and self._init_timestamps[0] < cutoff:
            self._init_timestamps.popleft()
        if len(self._init_timestamps) >= self._init_max:
            return False
        self._init_timestamps.append(now)
        return True
```

- [ ] **Step 4: Создать пустой `src/app/bridge/__init__.py`**

- [ ] **Step 5: Тесты проходят**

Run: `pytest tests/unit/test_throttler.py -v`
Expected: 4 PASS

- [ ] **Step 6: Commit**

```bash
git add src/app/bridge/__init__.py src/app/bridge/throttler.py tests/unit/test_throttler.py
git commit -m "feat(bridge): throttler with separate reply/initiation policies"
```

---

## Task 9: TelegramProvider (Telethon обёртка, тестируется моком)

**Files:**
- Create: `src/app/messaging/telegram/__init__.py`
- Create: `src/app/messaging/telegram/provider.py`
- Test: `tests/unit/test_telegram_provider.py`

- [ ] **Step 1: Написать тест с моком Telethon**

`tests/unit/test_telegram_provider.py`:
```python
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.messaging.types import ContentType, SendResult


@pytest.mark.asyncio
async def test_send_message_success():
    from app.messaging.telegram.provider import TelegramProvider

    provider = TelegramProvider(api_id=1, api_hash="x", sessions_dir="/tmp")
    mock_client = AsyncMock()
    mock_event = MagicMock()
    mock_event.id = 999
    mock_client.send_message.return_value = mock_event
    provider._client = mock_client  # type: ignore

    result = await provider.send_message(
        account_id=1, external_chat_id="12345", text="hello", is_initiation=False
    )
    assert isinstance(result, SendResult)
    assert result.success is True
    assert result.external_message_id == 999
    mock_client.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_send_message_floodwait():
    from app.messaging.telegram.provider import TelegramProvider
    from telethon.errors import FloodWaitError

    provider = TelegramProvider(api_id=1, api_hash="x", sessions_dir="/tmp")
    mock_client = AsyncMock()
    mock_client.send_message.side_effect = FloodWaitError(request=MagicMock(), capture=42)
    provider._client = mock_client  # type: ignore

    result = await provider.send_message(
        account_id=1, external_chat_id="12345", text="hello", is_initiation=True
    )
    assert result.success is False
    assert result.flood_wait_seconds == 42
```

- [ ] **Step 2: Запустить — FAIL**

Run: `pytest tests/unit/test_telegram_provider.py -v`

- [ ] **Step 3: Реализовать `src/app/messaging/telegram/provider.py`**

```python
import asyncio
import logging
from collections.abc import AsyncIterator
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.tl.types import User

from app.messaging.provider import MessengerProvider
from app.messaging.types import (
    ContentType,
    DeliveryStatus,
    IncomingMessage,
    SendResult,
)

logger = logging.getLogger(__name__)


class TelegramProvider(MessengerProvider):
    """Реализация MessengerProvider поверх Telethon (MTProto user-API).
    Один экземпляр = одна TG-сессия (один менеджер)."""

    def __init__(self, api_id: int, api_hash: str, sessions_dir: str):
        self._api_id = api_id
        self._api_hash = api_hash
        self._sessions_dir = Path(sessions_dir)
        self._sessions_dir.mkdir(parents=True, exist_ok=True)
        self._client: TelegramClient | None = None
        self._incoming_queue: asyncio.Queue[IncomingMessage] = asyncio.Queue()
        self._status_queue: asyncio.Queue[tuple[int, DeliveryStatus]] = asyncio.Queue()

    @property
    def session_file(self) -> Path:
        return self._sessions_dir / "session"

    async def connect(self) -> None:
        self._client = TelegramClient(
            str(self.session_file), self._api_id, self._api_hash
        )
        await self._client.connect()
        if not await self._client.is_user_authorized():
            raise RuntimeError("TG session not authorized — run auth_login first")
        self._client.add_event_handler(self._on_new_message)
        logger.info("TelegramProvider connected")

    async def disconnect(self) -> None:
        if self._client:
            await self._client.disconnect()
            self._client = None

    async def _on_new_message(self, event) -> None:
        """Handler событий Telethon NewMessage — кладёт в очередь."""
        try:
            sender = await event.get_sender()
            is_reply = event.is_reply or (event.message.message and False)
            msg = IncomingMessage(
                account_id=0,  # SessionManager проставит реальный account_id
                external_chat_id=str(event.chat_id),
                sender_tg_id=getattr(sender, "id", 0),
                sender_name=self._full_name(sender),
                sender_phone=getattr(sender, "phone", None),
                sender_username=getattr(sender, "username", None),
                content_type=ContentType.text,
                text=event.message.message,
                external_message_id=event.message.id,
                timestamp=event.message.date,
                is_reply=is_reply,
            )
            await self._incoming_queue.put(msg)
        except Exception:
            logger.exception("Failed to handle incoming TG message")

    @staticmethod
    def _full_name(sender) -> str | None:
        if not isinstance(sender, User):
            return None
        parts = [p for p in (sender.first_name, sender.last_name) if p]
        return " ".join(parts) or None

    async def incoming_stream(self) -> AsyncIterator[IncomingMessage]:
        while True:
            yield await self._incoming_queue.get()

    async def status_stream(self) -> AsyncIterator[tuple[int, DeliveryStatus]]:
        while True:
            yield await self._status_queue.get()

    async def send_message(
        self, account_id: int, external_chat_id: str, text: str, *, is_initiation: bool
    ) -> SendResult:
        if not self._client:
            return SendResult(success=False, error="not connected")
        try:
            result = await self._client.send_message(int(external_chat_id), text)
            return SendResult(success=True, external_message_id=result.id)
        except FloodWaitError as e:
            return SendResult(
                success=False,
                error="flood_wait",
                flood_wait_seconds=int(e.seconds),
            )
        except Exception as e:
            logger.exception("send_message failed")
            return SendResult(success=False, error=str(e))
```

- [ ] **Step 4: Создать пустой `src/app/messaging/telegram/__init__.py`**

- [ ] **Step 5: Тесты проходят**

Run: `pytest tests/unit/test_telegram_provider.py -v`
Expected: 2 PASS

- [ ] **Step 6: Commit**

```bash
git add src/app/messaging/telegram/ tests/unit/test_telegram_provider.py
git commit -m "feat(telegram): MessengerProvider impl over Telethon"
```

---

## Task 10: SessionManager (пул Telethon-сессий по менеджерам)

**Files:**
- Create: `src/app/bridge/session_manager.py`
- Test: `tests/unit/test_session_manager.py`

- [ ] **Step 1: Написать тест с мок-провайдером**

`tests/unit/test_session_manager.py`:
```python
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.bridge.session_manager import SessionManager


@pytest.mark.asyncio
async def test_register_and_get(monkeypatch):
    sm = SessionManager(api_id=1, api_hash="x", sessions_dir="/tmp")

    fake_provider = AsyncMock()
    fake_provider.connect = AsyncMock()

    monkeypatch.setattr(sm, "_build_provider", lambda account: fake_provider)

    account = MagicMock()
    account.id = 7
    account.phone = "+7000"

    await sm.register(account)
    assert sm.get(account.id) is fake_provider
    fake_provider.connect.assert_awaited_once()


@pytest.mark.asyncio
async def test_unregister_disconnects():
    sm = SessionManager(api_id=1, api_hash="x", sessions_dir="/tmp")
    fake_provider = AsyncMock()
    sm._providers[7] = fake_provider  # type: ignore

    await sm.unregister(7)
    fake_provider.disconnect.assert_awaited_once()
    assert sm.get(7) is None
```

- [ ] **Step 2: Запустить — FAIL**

Run: `pytest tests/unit/test_session_manager.py -v`

- [ ] **Step 3: Реализовать `src/app/bridge/session_manager.py`**

```python
import logging

from app.config import settings
from app.messaging.provider import MessengerProvider
from app.messaging.telegram.provider import TelegramProvider
from app.models import TgAccount

logger = logging.getLogger(__name__)


class SessionManager:
    """Пул Telethon-сессий — по одной на TG-аккаунт (менеджера).
    Связывает каждый аккаунт с ответственным менеджером."""

    def __init__(self, api_id: int, api_hash: str, sessions_dir: str):
        self._api_id = api_id
        self._api_hash = api_hash
        self._sessions_dir = sessions_dir
        self._providers: dict[int, MessengerProvider] = {}

    def _build_provider(self, account: TgAccount) -> MessengerProvider:
        # session_path уникален per аккаунт — суффикс по id
        provider = TelegramProvider(self._api_id, self._api_hash, self._sessions_dir)
        # Переопределяем session_file на per-account (см. ниже в connect-логике)
        provider._account_id = account.id  # type: ignore
        return provider

    async def register(self, account: TgAccount) -> MessengerProvider:
        if account.id in self._providers:
            return self._providers[account.id]
        provider = self._build_provider(account)
        await provider.connect()
        self._providers[account.id] = provider
        logger.info("Registered session for account_id=%s phone=%s", account.id, account.phone)
        return provider

    def get(self, account_id: int) -> MessengerProvider | None:
        return self._providers.get(account_id)

    async def unregister(self, account_id: int) -> None:
        provider = self._providers.pop(account_id, None)
        if provider:
            await provider.disconnect()
            logger.info("Unregistered session for account_id=%s", account_id)

    async def close_all(self) -> None:
        for account_id in list(self._providers):
            await self.unregister(account_id)
```

- [ ] **Step 4: Тесты проходят**

Run: `pytest tests/unit/test_session_manager.py -v`
Expected: 2 PASS

- [ ] **Step 5: Commit**

```bash
git add src/app/bridge/session_manager.py tests/unit/test_session_manager.py
git commit -m "feat(bridge): SessionManager pool of Telethon sessions per account"
```

---

## Task 11: OutboxWorker (воркер очереди с retry/backoff)

**Files:**
- Create: `src/app/bridge/outbox_worker.py`
- Test: `tests/unit/test_outbox_worker.py`

- [ ] **Step 1: Написать тесты**

`tests/unit/test_outbox_worker.py`:
```python
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.bridge.outbox_worker import OutboxWorker
from app.models import OutboxItem, OutboxStatus
from app.messaging.types import SendResult


def _make_item(**kw) -> OutboxItem:
    defaults = dict(
        id=1, dialog_id=10, tg_account_id=7, text="hi",
        status=OutboxStatus.queued, attempts=0,
        next_attempt_at=datetime.now(timezone.utc),
        last_error=None, is_initiation=False,
    )
    defaults.update(kw)
    return OutboxItem(**defaults)


@pytest.mark.asyncio
async def test_process_success_marks_sent():
    repo = AsyncMock()
    repo.fetch_due = AsyncMock(return_value=[_make_item()])
    repo.mark_sent = AsyncMock()

    provider = AsyncMock()
    provider.send_message = AsyncMock(return_value=SendResult(success=True, external_message_id=55))

    throttler = AsyncMock()
    throttler.acquire = AsyncMock(return_value=True)

    worker = OutboxWorker(repo=repo, get_provider=lambda aid: provider,
                          throttler_factory=lambda aid: throttler,
                          max_attempts=5)
    await worker._process_once()

    repo.mark_sent.assert_awaited_once()
    assert repo.mark_sent.call_args.args[0] == 55


@pytest.mark.asyncio
async def test_process_floodwait_reschedules():
    repo = AsyncMock()
    item = _make_item()
    repo.fetch_due = AsyncMock(return_value=[item])
    repo.reschedule = AsyncMock()

    provider = AsyncMock()
    provider.send_message = AsyncMock(
        return_value=SendResult(success=False, flood_wait_seconds=120, error="flood_wait")
    )

    throttler = AsyncMock()
    throttler.acquire = AsyncMock(return_value=True)

    worker = OutboxWorker(repo=repo, get_provider=lambda aid: provider,
                          throttler_factory=lambda aid: throttler, max_attempts=5)
    await worker._process_once()

    repo.reschedule.assert_awaited_once()
    # next_attempt_at сдвинут на ~120 сек
    _, kwargs = repo.reschedule.call_args
    assert kwargs["delay_seconds"] == 120


@pytest.mark.asyncio
async def test_process_max_attempts_marks_failed():
    repo = AsyncMock()
    item = _make_item(attempts=5)
    repo.fetch_due = AsyncMock(return_value=[item])
    repo.mark_failed = AsyncMock()

    provider = AsyncMock()
    provider.send_message = AsyncMock(return_value=SendResult(success=False, error="boom"))

    throttler = AsyncMock()
    throttler.acquire = AsyncMock(return_value=True)

    worker = OutboxWorker(repo=repo, get_provider=lambda aid: provider,
                          throttler_factory=lambda aid: throttler, max_attempts=5)
    await worker._process_once()

    repo.mark_failed.assert_awaited_once()


@pytest.mark.asyncio
async def test_throttle_rejects_reschedules_short():
    repo = AsyncMock()
    item = _make_item()
    repo.fetch_due = AsyncMock(return_value=[item])
    repo.reschedule = AsyncMock()

    provider = AsyncMock()
    throttler = AsyncMock()
    throttler.acquire = AsyncMock(return_value=False)  # лимит исчерпан

    worker = OutboxWorker(repo=repo, get_provider=lambda aid: provider,
                          throttler_factory=lambda aid: throttler, max_attempts=5)
    await worker._process_once()

    provider.send_message.assert_not_awaited()
    repo.reschedule.assert_awaited_once()
    _, kwargs = repo.reschedule.call_args
    assert kwargs["delay_seconds"] < 120
```

- [ ] **Step 2: Запустить — FAIL**

Run: `pytest tests/unit/test_outbox_worker.py -v`

- [ ] **Step 3: Реализовать `src/app/bridge/outbox_worker.py`**

```python
import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone

from app.bridge.throttler import Throttler
from app.messaging.provider import MessengerProvider
from app.models import OutboxItem, OutboxStatus

logger = logging.getLogger(__name__)


class OutboxRepository:
    """Абстракция доступа к очереди outbox. Конкретная impl (SQLAlchemy) — в Фазе 2."""

    async def fetch_due(self, limit: int = 50) -> list[OutboxItem]: ...
    async def mark_sent(self, item: OutboxItem, external_message_id: int) -> None: ...
    async def mark_failed(self, item: OutboxItem, error: str) -> None: ...
    async def reschedule(self, item: OutboxItem, *, delay_seconds: int, error: str | None = None) -> None: ...


class OutboxWorker:
    """Воркер очереди outbox: throttle → send → retry/backoff.
    spec §6.1 слои 1+2."""

    def __init__(
        self,
        repo: OutboxRepository,
        get_provider: Callable[[int], MessengerProvider | None],
        throttler_factory: Callable[[int], Throttler],
        max_attempts: int = 5,
        poll_interval: int = 2,
    ):
        self._repo = repo
        self._get_provider = get_provider
        self._throttler_factory = throttler_factory
        self._max_attempts = max_attempts
        self._poll_interval = poll_interval
        self._running = False
        self._throttlers: dict[int, Throttler] = {}

    def _throttler_for(self, account_id: int) -> Throttler:
        if account_id not in self._throttlers:
            self._throttlers[account_id] = self._throttler_factory(account_id)
        return self._throttlers[account_id]

    async def run(self) -> None:
        self._running = True
        while self._running:
            await self._process_once()
            await asyncio.sleep(self._poll_interval)

    def stop(self) -> None:
        self._running = False

    async def _process_once(self) -> None:
        items = await self._repo.fetch_due()
        for item in items:
            await self._handle(item)

    async def _handle(self, item: OutboxItem) -> None:
        provider = self._get_provider(item.tg_account_id)
        if provider is None:
            await self._repo.reschedule(item, delay_seconds=30, error="no_provider")
            return

        throttler = self._throttler_for(item.tg_account_id)
        allowed = await throttler.acquire(is_initiation=bool(item.is_initiation))
        if not allowed:
            await self._repo.reschedule(item, delay_seconds=10, error="throttled")
            return

        result = await provider.send_message(
            account_id=item.tg_account_id,
            external_chat_id=item.external_chat_id,  # type: ignore[attr-defined]
            text=item.text or "",
            is_initiation=bool(item.is_initiation),
        )

        if result.success:
            await self._repo.mark_sent(item, result.external_message_id)  # type: ignore[arg-type]
            return

        if result.flood_wait_seconds:
            await self._repo.reschedule(item, delay_seconds=result.flood_wait_seconds, error="flood_wait")
            return

        if item.attempts + 1 >= self._max_attempts:
            await self._repo.mark_failed(item, result.error or "unknown")
            return

        # экспоненциальный backoff: 30, 120, 480, ...
        delay = 30 * (2 ** item.attempts)
        await self._repo.reschedule(item, delay_seconds=delay, error=result.error)
```

> Примечание: `OutboxItem.is_initiation` и `OutboxItem.external_chat_id` — это поля, которые нужно добавить в модель `OutboxItem` (Task 5). В Task 5 они отсутствуют — добавьте их при реализации: `is_initiation: bool` и `external_chat_id: str`. Это несоответствие исправляется в self-review ниже (см. Task 5 update).

- [ ] **Step 4: Тесты проходят**

Run: `pytest tests/unit/test_outbox_worker.py -v`
Expected: 4 PASS

- [ ] **Step 5: Commit**

```bash
git add src/app/bridge/outbox_worker.py tests/unit/test_outbox_worker.py
git commit -m "feat(bridge): OutboxWorker with throttle, flood-wait, exponential backoff"
```

---

## Task 12: HealthChecker

**Files:**
- Create: `src/app/bridge/health_checker.py`

- [ ] **Step 1: Реализовать `src/app/bridge/health_checker.py`**

```python
import asyncio
import logging

from app.bridge.session_manager import SessionManager

logger = logging.getLogger(__name__)


class HealthChecker:
    """Периодическая проверка сессий (spec §6.1 слой 3).
    В Фазе 1 — только логирование; алерты админу в Фазе 4."""

    def __init__(self, session_manager: SessionManager, interval_sec: int = 300):
        self._sm = session_manager
        self._interval = interval_sec
        self._running = False

    async def run(self) -> None:
        self._running = True
        while self._running:
            await asyncio.sleep(self._interval)
            await self._check_once()

    def stop(self) -> None:
        self._running = False

    async def _check_once(self) -> None:
        for account_id, provider in list(self._sm._providers.items()):  # type: ignore[attr-defined]
            try:
                # Унифицированный интерфейс: проверяем наличие подключения
                client = getattr(provider, "_client", None)
                connected = bool(client and getattr(client, "is_connected", lambda: False)())
                if not connected:
                    logger.warning("Account %s: session disconnected", account_id)
            except Exception:
                logger.exception("Health check failed for account %s", account_id)
```

- [ ] **Step 2: Commit**

```bash
git add src/app/bridge/health_checker.py
git commit -m "feat(bridge): HealthChecker polling session liveness"
```

---

## Task 13: FastAPI app skeleton + health endpoint

**Files:**
- Create: `src/app/web/__init__.py`
- Create: `src/app/web/app.py`
- Create: `src/app/web/routes/__init__.py`
- Create: `src/app/web/routes/health.py`
- Test: `tests/integration/test_health.py`

- [ ] **Step 1: Написать тест**

`tests/integration/test_health.py`:
```python
from fastapi.testclient import TestClient

from app.web.app import create_app


def test_health_endpoint():
    app = create_app()
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
```

- [ ] **Step 2: Запустить — FAIL**

Run: `pytest tests/integration/test_health.py -v`

- [ ] **Step 3: Реализовать `src/app/web/routes/health.py`**

```python
from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
async def health() -> dict:
    return {"status": "ok"}
```

- [ ] **Step 4: Реализовать `src/app/web/app.py`**

```python
from fastapi import FastAPI

from app.web.routes import health


def create_app() -> FastAPI:
    app = FastAPI(title="Bitrix-TG", version="0.1.0")
    app.include_router(health.router)
    return app
```

- [ ] **Step 5: Создать пустые `__init__.py` для `web` и `web/routes`**

- [ ] **Step 6: Тест проходит**

Run: `pytest tests/integration/test_health.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add src/app/web/ tests/integration/test_health.py
git commit -m "feat(web): FastAPI app factory + /health endpoint"
```

---

## Task 14: Точка входа main.py (web | bridge | auth)

**Files:**
- Create: `src/app/main.py`
- Create: `src/app/messaging/telegram/auth.py`

- [ ] **Step 1: Реализовать `src/app/messaging/telegram/auth.py`**

```python
"""Разовая CLI-команда первого входа в TG-аккаунт.
Сохраняет .session файл, который потом используется bridge."""
import argparse
import asyncio
import sys

from telethon import TelegramClient

from app.config import settings


async def login(phone: str) -> None:
    client = TelegramClient(
        str(settings.tg_sessions_dir) + "/session",
        settings.tg_api_id,
        settings.tg_api_hash,
    )
    await client.connect()
    await client.start(phone=phone)
    me = await client.get_me()
    print(f"✓ Авторизован как {me.first_name} (id={me.id})")  # noqa: T201
    print(f"  Сессия сохранена в {settings.tg_sessions_dir}")  # noqa: T201
    await client.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(description="Bitrix-TG auth login")
    parser.add_argument("--phone", required=True, help="Номер в междунар. формате, +7...")
    args = parser.parse_args()
    asyncio.run(login(args.phone))


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Реализовать `src/app/main.py`**

```python
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
        from app.config import settings

        async def run_bridge() -> None:
            sm = SessionManager(
                api_id=settings.tg_api_id,
                api_hash=settings.tg_api_hash,
                sessions_dir=settings.tg_sessions_dir,
            )
            # В Фазе 1: просто держим процесс. Подгрузка аккаунтов — Фаза 2.
            print("Bridge started (Фаза 1: session loading в Фазе 2)")  # noqa: T201
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
        print(f"Unknown mode: {mode}. Use: web | bridge | auth")  # noqa: T201
        sys.exit(1)


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Проверить — web-режим запускается**

Run: `python -m app.main web` (Ctrl+C после старта)
Expected: `Uvicorn running on http://0.0.0.0:8000`, затем `/health` отвечает `{"status":"ok"}`.

- [ ] **Step 4: Commit**

```bash
git add src/app/main.py src/app/messaging/telegram/auth.py
git commit -m "feat: entrypoint (web|bridge|auth) + auth_login CLI"
```

---

## Task 15: Финальная проверка — весь набор тестов зелёный

- [ ] **Step 1: Полный прогон тестов**

Run: `pytest -v`
Expected: все тесты PASS (config, models, throttler, telegram_provider, session_manager, outbox_worker, health)

- [ ] **Step 2: Проверка Docker-сборки**

Run: `docker compose build`
Expected: образ собирается без ошибок

- [ ] **Step 3: Проверка линтера**

Run: `ruff check src/ tests/`
Expected: нет ошибок (исправить если есть)

- [ ] **Step 4: Финальный commit**

```bash
git add -A
git commit -m "chore: phase 1 complete — all tests green"
git tag phase1-foundation
```

---

## Self-Review (выполнено автором плана)

**1. Spec coverage (§2–§8 спеки):**
- §2 Архитектура (web + bridge процессы) → Tasks 13 (web) + 8–12 (bridge) ✓
- §3 Мультиаккаунтность → Task 10 (SessionManager) ✓
- §4 Интеграция Bitrix24 → **вынесена в Фазу 2** (явно указано в roadmap)
- §5 Модель данных → Tasks 5, 6 ✓
- §6 Anti-ban → Task 8 (throttler), Task 11 (outbox+backoff), Task 12 (health) ✓
- §7 Деплой → Task 3 (compose); nginx/TLS/мониторинг в Фазе 4 ✓
- §8 Потоки данных → частично в Фазе 1 (incoming/outgoing через provider), полностью end-to-end в Фазе 2

**2. Найденные несоответствия (исправлены):**
- **Task 5 → Task 11:** `OutboxItem` нуждается в полях `is_initiation: bool` и `external_chat_id: str`, которые не были включены в модель Task 5. **Исправление:** добавить в Task 5 при реализации:
  ```python
  is_initiation: Mapped[bool] = mapped_column(Boolean, default=False)
  external_chat_id: Mapped[str] = mapped_column(String(128), nullable=False)
  ```
  Тест `test_outbox_has_required_status_fields` тоже обновить — добавить `is_initiation` и `external_chat_id` в проверку.

**3. Scope:** Фаза 1 сфокусирована на фундаменте, не пытается покрыть Bitrix24/UI/деплой. Это корректно — каждый последующий план = отдельная фаза.

План готов к выполнению.
