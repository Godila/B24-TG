"""Разовая CLI-команда первого входа в TG-аккаунт.
Сохраняет .session файл, который потом используется bridge."""

import argparse
import asyncio

from telethon import TelegramClient

from app.config import get_settings


async def login(phone: str) -> None:
    settings = get_settings()
    client = TelegramClient(
        str(settings.tg_sessions_dir) + "/session",
        settings.tg_api_id,
        settings.tg_api_hash,
    )
    await client.connect()
    await client.start(phone=phone)
    me = await client.get_me()
    print(f"✓ Авторизован как {me.first_name} (id={me.id})")
    print(f"  Сессия сохранена в {settings.tg_sessions_dir}")
    await client.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(description="Bitrix-TG auth login")
    parser.add_argument("--phone", required=True, help="Номер в междунар. формате, +7...")
    args = parser.parse_args()
    asyncio.run(login(args.phone))


if __name__ == "__main__":
    main()
