"""Спайк: op 46 CONTACT_INFO_BY_PHONE на живом токене MAX («написать первым»).

Одно короткоживущее соединение (onboarding-клиент), один LOGIN — лимит
«30-50 быстрых LOGIN» не задет. Ничего не отправляет и не пишет в БД.

Запуск (VM): docker compose cp scripts/spike_max_op46.py bridge:/tmp/ &&
  docker compose exec bridge python /tmp/spike_max_op46.py [+7999…]
Без аргумента пробует собственные номера из аккаунтов (плейсхолдеры
MAX-<id> отфильтрованы — резолва не будет, только формат ответа).
"""

import asyncio
import logging
import sys

from sqlalchemy import select

from app.config import get_settings
from app.db import async_session
from app.messaging.max.factory import make_onboarding_client
from app.messaging.max.protocol import (
    DEFAULT_APP_VERSION,
    DEFAULT_BROWSER_UA,
    OP_CONTACT_INFO_BY_PHONE,
    OP_INIT,
    OP_LOGIN,
    build_user_agent,
    init_payload,
    login_payload,
    to_int,
)
from app.models import TgAccount

logging.basicConfig(level=logging.WARNING)


def _mask(phone: str | None) -> str:
    return f"***{phone[-4:]}" if phone else "None"


async def probe(client, phone: str) -> dict | None:
    """Ответ op46; {'contact': {id…}} | None (не найден/приватность)."""
    resp = await client.request(OP_CONTACT_INFO_BY_PHONE, {"phone": phone})
    contact = (resp.get("payload") or {}).get("contact")
    return contact if isinstance(contact, dict) and contact else None


async def main() -> None:
    phones = sys.argv[1:]
    settings = get_settings()
    async with async_session() as session:
        acc = (
            await session.execute(
                select(TgAccount).where(
                    TgAccount.messenger == "max",
                    TgAccount.is_removed.is_(False),
                )
            )
        ).scalar_one()
    if not phones:
        phones = [acc.phone or ""]
    print(f"account id={acc.id} max_user_id={acc.max_user_id} phone={_mask(acc.phone)}")

    client = make_onboarding_client()
    await client.connect()
    try:
        await client.request(
            OP_INIT,
            init_payload(
                acc.device_id,
                build_user_agent(
                    settings.max_app_version or DEFAULT_APP_VERSION,
                    settings.max_browser_ua or DEFAULT_BROWSER_UA,
                ),
            ),
        )
        await client.request(OP_LOGIN, login_payload(acc.token))
        print("LOGIN ok")
        for phone in phones:
            contact = await probe(client, phone)
            uid = to_int((contact or {}).get("id"))
            print(f"{_mask(phone)} -> contact_id={uid}")
            # Живой чек контракта: найденный контакт обязан иметь int-id,
            # XOR chatId (= own ^ peer) считается провайдером из них.
            if phone.startswith("+7") and phone[1:].isdigit():
                assert uid is None or uid > 0
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
