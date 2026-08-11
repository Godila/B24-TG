#!/usr/bin/env python3
"""Регистрация placement-виджета в Bitrix24 (один раз при установке).

Вызывает placement.bind для точки встраивания CRM_DEAL_DETAIL_TAB, указывая
на наш HTTPS handler-URL. После этого в карточке сделки появляется вкладка
«Telegram-чат».

Запуск на VM (с доступом к .env):
    docker compose exec web python /app/scripts/register_placement.py
"""
import asyncio
import os

from app.b24.client import Bitrix24Client
from app.b24.token_manager import TokenManager

PLACEMENT_CODE = "CRM_DEAL_DETAIL_TAB"
HANDLER_URL = "https://b24-tg.haragy.top/placement/deal"
TITLE = "Telegram-чат"


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
        result = await client.call(
            "placement.bind",
            auth_token=token.access_token,
            params={
                "PLACEMENT": PLACEMENT_CODE,
                "HANDLER": HANDLER_URL,
                "TITLE": TITLE,
                "LANG_ALL": {
                    "ru": {"TITLE": "Telegram-чат"},
                    "en": {"TITLE": "Telegram chat"},
                },
            },
        )
        print(f"placement.bind result: {result}")
        if result:
            print(f"SUCCESS: вкладка «{TITLE}» зарегистрирована в карточке сделки.")
            print(f"Handler: {HANDLER_URL}")
        else:
            print("WARNING: placement.bind returned False (maybe already bound).")
    except Exception as e:
        print(f"placement.bind error: {e}")
        # Если уже зарегистрирован — покажем текущие bindings.
        print("\nТекущие placement handlers:")
        try:
            bindings = await client.call(
                "placement.get", auth_token=token.access_token,
            )
            for b in (bindings or []):
                if "DEAL" in str(b.get("placement", "")):
                    print(f"  {b}")
        except Exception as e2:
            print(f"  (не удалось получить placement.get: {e2})")


if __name__ == "__main__":
    asyncio.run(main())
