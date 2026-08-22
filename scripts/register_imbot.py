#!/usr/bin/env python3
"""Ручная регистрация чат-бота «ЧатМост» (обёртка над app.b24.imbot).

Авто-регистрация уже встроена в ONAPPINSTALL (webhook.py); этот скрипт —
для разового прогона без переустановки или диагностики:

    docker compose exec web python /app/scripts/register_imbot.py

Пишет id и в app_settings (источник для воркера), и печатает для .env.
Один assert по AGENTS.
"""

import asyncio
import sys

from sqlalchemy import select

from app.b24.client import Bitrix24Client
from app.b24.imbot import KEY_IMBOT_BOT_ID, ensure_bot_registered
from app.b24.token_manager import TokenManager
from app.config import get_settings
from app.db import async_session
from app.models import AppSetting


async def main() -> None:
    s = get_settings()
    if not s.public_base_url:
        print("FAIL: PUBLIC_BASE_URL пуст — некуда вешать webhook событий бота")
        sys.exit(1)
    client = Bitrix24Client(
        client_endpoint=s.b24_portal.rstrip("/") + "/rest/",
        min_interval=s.b24_min_call_interval,
    )
    tm = TokenManager(client_id=s.b24_client_id, client_secret=s.b24_client_secret)
    token = await tm.get_token()
    if token is None:
        print("FAIL: no B24 token")
        sys.exit(1)

    bot_id = await ensure_bot_registered(
        client,
        token.access_token,
        webhook_url=s.public_base_url.rstrip("/") + "/webhook/b24/imbot",
    )
    await client.aclose()
    assert bot_id > 0  # единственный живой чек контракта (AGENTS)

    async with async_session() as session:
        row = (
            await session.execute(
                select(AppSetting).where(AppSetting.key == KEY_IMBOT_BOT_ID)
            )
        ).scalar_one_or_none()
        if row is None:
            session.add(AppSetting(key=KEY_IMBOT_BOT_ID, value=str(bot_id)))
        else:
            row.value = str(bot_id)
        await session.commit()

    print(f"BOT ok: id={bot_id} (app_settings обновлён; env IMBOT_BOT_ID опционален)")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
