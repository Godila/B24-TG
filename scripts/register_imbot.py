#!/usr/bin/env python3
"""Регистрация чат-бота «ЧатМост» (imbot.v2) + команды dismiss.

Запуск внутри web-контейнера на VM ПОСЛЕ выдачи права «Чат-боты» (imbot)
и переустановки приложения (webhook /webhook/b24/imbot уже задеплоен):

    docker compose exec web python /app/scripts/register_imbot.py

Идемпотентен (Bot.register/Command.register повторным вызовом возвращают
существующих). Итог: печатает bot_id — записать в .env как
IMBOT_BOT_ID=<id> и перезапустить web+bridge. Один assert по AGENTS.
"""

import asyncio
import sys

from app.b24.client import Bitrix24Client
from app.b24.token_manager import TokenManager
from app.config import get_settings

BOT_CODE = "chatmost"
COMMAND = "dismiss"


async def main() -> None:
    s = get_settings()
    client = Bitrix24Client(
        client_endpoint=s.b24_portal.rstrip("/") + "/rest/",
        min_interval=s.b24_min_call_interval,
    )
    tm = TokenManager(client_id=s.b24_client_id, client_secret=s.b24_client_secret)
    token = await tm.get_token()
    if token is None:
        print("FAIL: no B24 token")
        sys.exit(1)
    auth = token.access_token

    if not s.public_base_url:
        print("FAIL: PUBLIC_BASE_URL пуст — некуда вешать webhook событий бота")
        sys.exit(1)

    bot = await client.call(
        "imbot.v2.Bot.register",
        auth_token=auth,
        params={
            "fields": {
                "code": BOT_CODE,
                "properties": {
                    "name": "ЧатМост",
                    "workPosition": "Уведомления о сообщениях клиентов",
                },
                "type": "bot",
                "eventMode": "webhook",
                "webhookUrl": s.public_base_url.rstrip("/") + "/webhook/b24/imbot",
            }
        },
    )
    bot_id = int(bot["bot"]["id"])
    # Единственный живой чек контракта (AGENTS: спайк/скрипт — один assert).
    assert bot_id > 0
    print(f"BOT registered: id={bot_id} code={bot['bot'].get('code')}")

    cmd = await client.call(
        "imbot.v2.Command.register",
        auth_token=auth,
        params={
            "botId": bot_id,
            "fields": {
                "command": COMMAND,
                "title": {"ru": "Отвечать не нужно (ЧатМост)"},
                "hidden": True,
                "common": False,
            },
        },
    )
    print(f"COMMAND registered: {cmd.get('command', {}).get('command')}")

    await client.aclose()
    print(f"NEXT: добавь в .env -> IMBOT_BOT_ID={bot_id}, затем перезапусти web+bridge")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
