#!/usr/bin/env python3
"""Привязка пункта «Чаты» (общий мессенджер) в левое меню Битрикс24.

B24 позволяет НЕСКОЛЬКО обработчиков одного placement — они различаются
handler-URL. Тело POST у обоих LEFT_MENU одинаковое (PLACEMENT=LEFT_MENU),
роуты различаем по URL: /placement/admin (панель управления) и
/placement/chats (этот пункт, отдаёт static/inbox.html).

Идемпотентно: placement.get → биндинг с нашим handler уже есть? выходим :
placement.bind. Биндинг админки не трогает. Поиск именно по handler (а не
по коду placement) — ключей LEFT_MENU теперь два, свёртка в dict по коду
коллизирует.

Запуск на VM (с доступом к .env):
    docker compose exec web python /app/scripts/bind_chats_placement.py
"""

import asyncio
import os

from app.b24.client import Bitrix24Client
from app.b24.token_manager import TokenManager

BASE_URL = "https://b24-tg.haragy.top"
HANDLER = f"{BASE_URL}/placement/chats"

BIND_PARAMS = {
    "PLACEMENT": "LEFT_MENU",
    "HANDLER": HANDLER,
    "TITLE": "Чаты",
    "LANG_ALL": {
        "ru": {"TITLE": "Чаты"},
        "en": {"TITLE": "Chats"},
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
        # Ключ идемпотентности — handler-URL: LEFT_MENU-биндингов два.
        ours = [b for b in bindings if str(b.get("handler", "")) == HANDLER]
        if ours:
            print(f"OK: пункт «Чаты» уже привязан ({HANDLER}) — ничего не делаем.")
            return
        result = await client.call(
            "placement.bind",
            auth_token=token.access_token,
            params=BIND_PARAMS,
        )
        print(f"Привязан «Чаты» ({HANDLER}), result={result}")
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
