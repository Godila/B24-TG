#!/usr/bin/env python3
"""Приводит левое меню Битрикс24 к единственному пункту «ЧатМост».

B24 рендерит ОДИН пункт LEFT_MENU на приложение, сколько бы обработчиков
ни было привязано (живая проверка 2026-08-17: биндинг «Чаты» существовал,
пункт в меню не появился). Поэтому пункт меню ведёт на оболочку
/placement/app (вкладки «Чаты»/«Панель»), а legacy-обработчики
/placement/admin и /placement/chats отвязываем — они остаются рабочими
ссылками/iframe-источниками, но в меню не дублируются.

Идемпотентно: bind /placement/app если его нет; unbind legacy-хендлеры,
если ещё привязаны. CRM_DEAL_DETAIL_TAB не трогает.

Запуск на VM (с доступом к .env):
    docker compose exec web python /app/scripts/bind_chats_placement.py
"""

import asyncio
import os

from app.b24.client import Bitrix24Client
from app.b24.token_manager import TokenManager

BASE_URL = "https://b24-tg.haragy.top"
HANDLER = f"{BASE_URL}/placement/app"
LEGACY_HANDLERS = [
    f"{BASE_URL}/placement/admin",
    f"{BASE_URL}/placement/chats",
]

BIND_PARAMS = {
    "PLACEMENT": "LEFT_MENU",
    "HANDLER": HANDLER,
    "TITLE": "ЧатМост",
    "LANG_ALL": {
        "ru": {"TITLE": "ЧатМост"},
        "en": {"TITLE": "ChatMost"},
    },
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
        bindings = (
            await client.call(
                "placement.get",
                auth_token=token.access_token,
            )
            or []
        )
        left_menu = [
            str(b.get("handler", "")) for b in bindings if b.get("placement") == "LEFT_MENU"
        ]

        if HANDLER in left_menu:
            print(f"OK: LEFT_MENU уже ведёт на {HANDLER} — ничего не делаем.")
        else:
            result = await client.call(
                "placement.bind",
                auth_token=token.access_token,
                params=BIND_PARAMS,
            )
            print(f"Привязана оболочка {HANDLER}, result={result}")

        for legacy in LEGACY_HANDLERS:
            if legacy in left_menu:
                await client.call(
                    "placement.unbind",
                    auth_token=token.access_token,
                    params={"PLACEMENT": "LEFT_MENU", "HANDLER": legacy},
                )
                print(f"Отвязан legacy-обработчик {legacy}")
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
