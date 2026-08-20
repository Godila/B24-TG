#!/usr/bin/env python3
"""Добавить канальные источники (TELEGRAM, MAX) в справочник CRM портала.

Карточки, созданные из диалогов, получают SOURCE_ID канала (см.
app/b24/channels.py). Ни TELEGRAM, ни MAX в стандартном справочнике портала
НЕТ — без записей create_* ретраит без источника (фолбэк в crm.py), и поле
«Источник» остаётся пустым (поймано живым тестом 2026-08-20).

Идемпотентно и без дублей: crm.status.list → запись с нужным STATUS_ID есть?
пропускаем; есть ПОХОЖАЯ ПО ИМЕНИ с другим кодом (NAME в B24 неуникален) —
не создаём, предлагаем выбрать её в панели ЧатМост; иначе crm.status.add.

Запуск на VM (с доступом к .env):
    docker compose exec web python /app/scripts/add_max_source.py
"""

import asyncio
import os

from app.b24.client import Bitrix24Client
from app.b24.sources import B24Source, fetch_sources, name_looks_like
from app.b24.token_manager import TokenManager
from app.models import Messenger

SOURCE_ENTITY = "SOURCE"
CHANNEL_SOURCES = {
    "TELEGRAM": "Telegram (мессенджер)",
    "MAX": "MAX (мессенджер)",
}


def duplicate_hint(entries: list[B24Source], status_id: str) -> B24Source | None:
    """Похожая по имени запись с ДРУГИМ кодом — созда поверх, получим дубль."""
    messenger = {"TELEGRAM": Messenger.tg, "MAX": Messenger.max}[status_id]
    for e in entries:
        if e.status_id.upper() != status_id and name_looks_like(e.name, messenger):
            return e
    return None


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
        sources = await fetch_sources(client, token.access_token)
        have = {s.status_id.upper() for s in sources}
        for status_id, name in CHANNEL_SOURCES.items():
            if status_id in have:
                print(f"OK: источник уже есть (STATUS_ID={status_id}) — пропускаем.")
                continue
            hint = duplicate_hint(sources, status_id)
            if hint is not None:
                print(
                    f"Похожая запись уже есть: {hint.name} ({hint.status_id}) — "
                    f"выберите её в панели ЧатМост; создание {status_id} пропущено."
                )
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
