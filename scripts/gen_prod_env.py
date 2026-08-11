#!/usr/bin/env python3
"""Генерация production .env на VM из шаблона + B24-токенов.

Читает /root/.b24_tokens.json, генерит случайные SESSION_SECRET /
POSTGRES_PASSWORD / B24_WEBHOOK_SECRET, пишет /opt/bitrix-tg/.env.
Запускается НА VM (один раз)."""
import json
import secrets
import string
from pathlib import Path

TOKENS_PATH = Path("/root/.b24_tokens.json")
ENV_PATH = Path("/opt/bitrix-tg/.env")

tokens = json.loads(TOKENS_PATH.read_text())

# Один пароль для postgres — используется и в DATABASE_URL, и в POSTGRES_PASSWORD.
db_password = secrets.token_urlsafe(16)


def rand_secret(n: int = 48) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(n))


env = f"""# Production environment — generated. НЕ коммитить.
# Portal: https://b24-ye2jjz.bitrix24.ru

# --- Bitrix24 OAuth ---
B24_PORTAL=https://b24-ye2jjz.bitrix24.ru
B24_CLIENT_ID=local.6a7a33e38edc66.87243082
B24_CLIENT_SECRET=Rtjv2t4Kc5OTeajukCGTq7SWNf9cBCL4XNAnFsjwPW4bCjgqia
B24_OAUTH_REDIRECT=https://b24-tg.haragy.top/app
B24_CLIENT_ENDPOINT=https://b24-ye2jjz.bitrix24.ru/rest/
B24_MEMBER_ID={tokens['member_id']}
B24_ACCESS_TOKEN={tokens['access_token']}
B24_REFRESH_TOKEN={tokens['refresh_token']}
B24_WEBHOOK_SECRET={rand_secret(32)}

# --- Telegram (MTProto) ---
TG_API_ID=12345
TG_API_HASH=your_api_hash_from_my_telegram_org
TG_SESSIONS_DIR=/data/tg_sessions

# --- Storage ---
# ВАЖНО: пароль в DATABASE_URL и POSTGRES_PASSWORD должен совпадать.
DATABASE_URL=postgresql+asyncpg://bitrix_tg:{db_password}@postgres:5432/bitrix_tg
POSTGRES_PASSWORD={db_password}
REDIS_URL=redis://redis:6379/0

# --- Web/UI ---
SESSION_SECRET={rand_secret(48)}
DEV_MODE=false
CORS_ORIGINS=https://b24-ye2jjz.bitrix24.ru,https://b24-tg.haragy.top
STATIC_DIR=/app/src/app/static

# --- Throttling ---
THROTTLE_INIT_MAX=10
THROTTLE_INIT_WINDOW=180
THROTTLE_INIT_MIN_INTERVAL=5
THROTTLE_REPLY_MAX=20

# --- Outbox ---
OUTBOX_POLL_INTERVAL=2
OUTBOX_MAX_ATTEMPTS=5

# --- Observability ---
SENTRY_DSN=
"""
ENV_PATH.write_text(env)
ENV_PATH.chmod(600)
print(f"Wrote {ENV_PATH} ({ENV_PATH.stat().st_size} bytes), chmod 600")
print("POSTGRES_PASSWORD and SESSION_SECRET regenerated randomly.")
