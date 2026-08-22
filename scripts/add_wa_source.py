#!/usr/bin/env python3
"""Добавить источник WHATSAPP в справочник CRM портала.

Клон add_max_source.py (канал WhatsApp, 2026-08-22): карточки из WA-диалогов
получают SOURCE_ID="WHATSAPP" (app/b24/channels.py); в стандартном
справочнике портала записи нет — без неё create_* ретраит без источника
(фолбэк в crm.py), поле «Источник» остаётся пустым.

Идемпотентно: crm.status.list → запись WHATSAPP есть? пропускаем; есть
ПОХОЖАЯ ПО ИМЕНИ с другим кодом — не создаём (подсказка); иначе crm.status.add.

Запуск на VM (с доступом к .env):
    docker compose exec web python /app/scripts/add_wa_source.py
"""

import asyncio
import os

from app.b24.client import Bitrix24Client
from app.b24.sources import B24Source, fetch_sources, name_looks_like
from app.b24.token_manager import TokenManager
from app.models import Messenger

STATUS_ID = "WHATSAPP"
SOURCE_ENTITY = "SOURCE"
SOURCE_NAME = "WhatsApp (мессенджер)"


def duplicate_hint(entries: list[B24Source]) -> B24Source | None:
    """Похожая по имени запись с ДРУГИМ кодом — создав поверх, получим дубль."""
    for e in entries:
        if e.status_id.upper() != STATUS_ID and name_looks_like(e.name, Messenger.wa):
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
        entries = await fetch_sources(client, token.access_token)
        if any(e.status_id.upper() == STATUS_ID for e in entries):
            print(f"OK: источник {STATUS_ID} уже есть в справочнике.")
            return
        dup = duplicate_hint(entries)
        if dup is not None:
            print(
                f"Похожая запись уже существует: {dup.status_id} «{dup.name}».\n"
                f"Новый код не создавал. Выберите её в панели ЧатМост или "
                f"удалите и перезапустите скрипт."
            )
            return
        result = await client.call(
            "crm.status.add",
            auth_token=token.access_token,
            params={
                "fields": {
                    "ENTITY_ID": SOURCE_ENTITY,
                    "STATUS_ID": STATUS_ID,
                    "NAME": SOURCE_NAME,
                    "SORT": 330,
                }
            },
        )
        print(f"Создан источник {STATUS_ID} «{SOURCE_NAME}», status-id={result}")
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
