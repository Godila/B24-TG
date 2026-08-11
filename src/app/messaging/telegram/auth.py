"""Разовая CLI-команда первого входа в TG-аккаунт.

Сохраняет .session файл в per-account подпапку (account_<id>/session),
именно туда, где его ищет SessionManager при регистрации аккаунта.
Поэтому bridge подхватит сессию без ручного перемещения файла.

Запуск:
    python -m app.main auth --phone +79991234567

Перед запуском аккаунт должен существовать в БД (таблица tg_accounts)
с указанным номером phone. Если нет — создать его (см. docs/DEPLOY.md).
"""

import argparse
import asyncio
from pathlib import Path

from sqlalchemy import select
from telethon import TelegramClient

from app.config import get_settings
from app.db import async_session
from app.models import TgAccount


async def login(phone: str) -> int:
    settings = get_settings()

    # Найти аккаунт по номеру — чтобы положить .session в account_<id>/.
    async with async_session() as s:
        result = await s.execute(select(TgAccount).where(TgAccount.phone == phone))
        account = result.scalar_one_or_none()
    if account is None:
        print(
            f"✗ Аккаунт с phone={phone} не найден в БД. "
            "Создайте запись в tg_accounts (scripts/seed_manager.py или вручную)."
        )
        return 1

    # Per-account подпапка — совпадает с SessionManager._build_provider.
    session_dir = Path(settings.tg_sessions_dir) / f"account_{account.id}"
    session_dir.mkdir(parents=True, exist_ok=True)
    session_path = session_dir / "session"

    client = TelegramClient(
        str(session_path), settings.tg_api_id, settings.tg_api_hash
    )
    await client.connect()
    # client.start сам спросит код подтверждения (SMS/Telegram) и 2FA пароль.
    await client.start(phone=phone)
    me = await client.get_me()
    print(f"✓ Авторизован как {me.first_name} (id={me.id})")
    print(f"  .session сохранён в {session_path}")
    await client.disconnect()
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Bitrix-TG auth login")
    parser.add_argument(
        "--phone", required=True, help="Номер аккаунта в БД, междунар. формат +7..."
    )
    args = parser.parse_args()
    raise SystemExit(asyncio.run(login(args.phone)))


if __name__ == "__main__":
    main()
