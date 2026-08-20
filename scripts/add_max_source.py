#!/usr/bin/env python3
"""Добавить канальные источники (TELEGRAM, MAX) в справочник CRM портала.

Карточки, созданные из диалогов, получают SOURCE_ID канала (см.
app/b24/channels.py). Ни TELEGRAM, ни MAX в стандартном справочнике портала
НЕТ — без записей create_* молча ретраит без источника (фолбэк в crm.py),
и поле «Источник» остаётся пустым (поймано живым тестом 2026-08-20).

Идемпотентно: crm.status.list → недостающие записи добавляем crm.status.add.

Запуск на VM (с доступом к .env):
    docker compose exec web python /app/scripts/add_max_source.py
"""

import asyncio
import os

from app.b24.client import Bitrix24Client
from app.b24.token_manager import TokenManager

SOURCE_ENTITY = "SOURCE"
CHANNEL_SOURCES = {
    "TELEGRAM": "Telegram (мессенджер)",
    "MAX": "MAX (мессенджер)",
}


async def main() -> None:
    client_id = os.environ["B24_CLIENT_ID"]
    client_secret = os.environ["B24_CLIENT_SECRET"]
    portal = os.environ["B24_PORTAL"].rstrip("/") + "/rest/"

    token_mgr = TokenManager(client_id=client_id, client_secret=client_secret)
    token = await token_mgr.get_token()
    if token is None:
        print("ERROR: B24 token not found — run app install or seed tokens first.")
        return

    client = Bitrix24Client(client_endpoint=portal)
    try:
        existing = await client.call(
            "crm.status.list",
            auth_token=token.access_token,
            params={"filter": {"ENTITY_ID": SOURCE_ENTITY}},
        )
        entries = existing if isinstance(existing, list) else existing.get("items", [])
        have = {str(e.get("STATUS_ID", "")).upper() for e in entries}
        for status_id, name in CHANNEL_SOURCES.items():
            if status_id in have:
                print(f"OK: источник уже есть (STATUS_ID={status_id}) — пропускаем.")
                continue
            result = await client.call(
                "crm.status.add",
                auth_token=token.access_token,
                params={
                    "fields": {
                        "ENTITY_ID": SOURCE_ENTITY,
                        "STATUS_ID": status_id,
                        "NAME": name,
                        "SORT": 500,
                    }
                },
            )
            print(f"Создан источник {status_id} ({name}), status-id={result}")
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
