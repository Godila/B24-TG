#!/usr/bin/env python3
"""Добавить источник «MAX» в справочник CRM-источников портала.

Контакты, созданные из MAX-диалогов, получают SOURCE_ID="MAX" (см.
app/b24/channels.py). Стандартного источника MAX в портале нет — без записи
контакты получали дефолтный «Звонок»/«CALL».

Идемпотентно: crm.status.list → запись с STATUS_ID="MAX" есть? выходим :
crm.status.add.

Запуск на VM (с доступом к .env):
    docker compose exec web python /app/scripts/add_max_source.py
"""

import asyncio
import os

from app.b24.client import Bitrix24Client
from app.b24.token_manager import TokenManager

SOURCE_ENTITY = "SOURCE"
SOURCE_STATUS_ID = "MAX"
SOURCE_NAME = "MAX (мессенджер)"


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
        for e in entries:
            if str(e.get("STATUS_ID", "")).upper() == SOURCE_STATUS_ID:
                print(
                    f"OK: источник уже есть (STATUS_ID={SOURCE_STATUS_ID}, "
                    f"NAME={e.get('NAME')}) — ничего не делаем."
                )
                return

        result = await client.call(
            "crm.status.add",
            auth_token=token.access_token,
            params={
                "fields": {
                    "ENTITY_ID": SOURCE_ENTITY,
                    "STATUS_ID": SOURCE_STATUS_ID,
                    "NAME": SOURCE_NAME,
                    "SORT": 500,
                }
            },
        )
        print(f"Создан источник {SOURCE_STATUS_ID} ({SOURCE_NAME}), status-id={result}")
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
