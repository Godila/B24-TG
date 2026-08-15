#!/usr/bin/env python3
"""Регистрация placement-виджетов в Bitrix24 (один раз при установке).

Биндит обе точки:
- CRM_DEAL_DETAIL_TAB → /placement/deal — вкладка «Чат» в карточке сделки;
- LEFT_MENU → /placement/admin — пункт «Мессенджеры» в главном меню
  портала (админ-панель внутри интерфейса B24; менеджерам — карточки
  подключения своих каналов, supervisor'у — панель управления).

Запуск на VM (с доступом к .env):
    docker compose exec web python /app/scripts/register_placement.py
"""
import asyncio
import os

from app.b24.client import Bitrix24Client
from app.b24.token_manager import TokenManager

BASE_URL = "https://b24-tg.haragy.top"

BINDINGS = [
    {
        "PLACEMENT": "CRM_DEAL_DETAIL_TAB",
        "HANDLER": f"{BASE_URL}/placement/deal",
        "TITLE": "Чат",
        "LANG_ALL": {
            "ru": {"TITLE": "Чат"},
            "en": {"TITLE": "Chat"},
        },
    },
    {
        "PLACEMENT": "LEFT_MENU",
        "HANDLER": f"{BASE_URL}/placement/admin",
        "TITLE": "Мессенджеры",
        "LANG_ALL": {
            "ru": {"TITLE": "Мессенджеры"},
            "en": {"TITLE": "Messengers"},
        },
    },
]


async def main():
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
        # Идемпотентность: повторный bind той же точки создаёт ДУБЛИКАТ
        # (две вкладки/два пункта меню), поэтому сначала смотрим, что уже
        # привязано, и биндим только отсутствующее.
        existing: dict[str, str] = {}
        try:
            for b in await client.call(
                "placement.get", auth_token=token.access_token,
            ) or []:
                existing[str(b.get("placement", ""))] = str(b.get("handler", ""))
        except Exception as e:  # noqa: BLE001 - one-off ops-скрипт
            print(f"placement.get error (продолжаем вслепую): {e}")

        for binding in BINDINGS:
            code = binding["PLACEMENT"]
            handler = binding["HANDLER"]
            if code in existing:
                if existing[code] == handler:
                    print(f"{code}: уже привязан к нашему handler'у — пропускаем")
                else:
                    print(f"{code}: привязан к ДРУГОМУ handler'у "
                          f"({existing[code]}) — пропускаем, разбирайтесь руками")
                continue
            try:
                result = await client.call(
                    "placement.bind", auth_token=token.access_token,
                    params=binding,
                )
                print(f"{code}: bind result={result}")
                if not result:
                    print(f"  WARNING: {code} returned False.")
            except Exception as e:  # noqa: BLE001 - one-off ops-скрипт
                print(f"{code}: bind error: {e}")
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
