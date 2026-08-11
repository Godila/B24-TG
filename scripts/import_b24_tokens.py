#!/usr/bin/env python3
"""Импорт B24-токенов из /root/.b24_tokens.json в таблицу b24_tokens.

Токены были получены headless-OAuth (Фаза 2) и лежат на VM. Этот скрипт
переносит их в БД, чтобы TokenManager нашёл их при первом запросе.

Запуск на VM:
    docker compose exec web python /app/scripts/import_b24_tokens.py
"""
import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from app.db import async_session
from app.models import B24Token

TOKENS_PATH = Path("/root/.b24_tokens.json")
# Если файл недоступен из контейнера — попробуем переменную окружения.
ALT_PATH = Path("/data/b24_tokens.json")


async def main():
    src = TOKENS_PATH if TOKENS_PATH.is_file() else ALT_PATH
    if not src.is_file():
        print(f"ERROR: tokens file not found at {TOKENS_PATH} or {ALT_PATH}")
        return
    data = json.loads(src.read_text())
    print(f"Loaded tokens from {src}: member_id={data.get('member_id')}")

    async with async_session() as s:
        existing = (
            await s.execute(select(B24Token).where(B24Token.member_id == data["member_id"]))
        ).scalar_one_or_none()
        # expires_in может прийти как число секунд или как ISO-строка 'expires'.
        expires_in = data.get("expires_in")
        if isinstance(expires_in, (int, float)):
            expires_at = datetime.now(UTC) + timedelta(seconds=expires_in)
        else:
            # Если access_token уже близко к истечению, refresh произойдёт
            # автоматически при первом get_token(). Ставим сейчас+1ч как ориентир.
            expires_at = datetime.now(UTC) + timedelta(hours=1)
        if existing is None:
            tok = B24Token(
                member_id=data["member_id"],
                access_token=data["access_token"],
                refresh_token=data["refresh_token"],
                client_endpoint=data.get(
                    "client_endpoint",
                    "https://b24-ye2jjz.bitrix24.ru/rest/",
                ),
                portal=data.get("domain", "b24-ye2jjz.bitrix24.ru"),
                user_id=int(data.get("user_id", 1)),
                scope=data.get("scope", ""),
                expires_at=expires_at,
            )
            s.add(tok)
            print(f"Inserted B24Token member_id={data['member_id']}")
        else:
            existing.access_token = data["access_token"]
            existing.refresh_token = data["refresh_token"]
            existing.client_endpoint = data.get(
                "client_endpoint", existing.client_endpoint
            )
            existing.expires_at = expires_at
            print(f"Updated B24Token member_id={data['member_id']}")
        await s.commit()
        print("Token import complete.")


if __name__ == "__main__":
    asyncio.run(main())
