#!/usr/bin/env python3
"""Бэкфилл данных каналов в уже существующие CRM-карточки контактов.

До перехода на crm.contact.add карточки создавались через crm.item.add,
который молча выбрасывал мульти-поля: телефоны и IM не сохранялись ни у
одного контакта (HAS_PHONE=N), из-за этого же не работал дедуп findbyComm.
Скрипт проходит по нашей БД и дозаполняет в B24 то, чего в карточке нет:

- телефон (Contact.phone → PHONE, если в карточке нет ни одного);
- @username (Contact.username → IM типа TELEGRAM, если IM пуст);
- split-имя: Contact.first_name/last_name, а для старых строк — эвристика
  «первое слово → NAME, остальное → LAST_NAME» (только когда LAST_NAME пуст).

Обновляет только пустые поля карточки — данные, внесённые менеджерами
вручную, не трогает. Идемпотентен, запуск на VM:
    docker compose exec web python /app/scripts/backfill_crm_contacts.py
"""

import asyncio
import os
from typing import Any

from sqlalchemy import select

from app.b24.client import Bitrix24Client
from app.b24.token_manager import TokenManager
from app.db import async_session
from app.models import Contact


def _split_name_fallback(full_name: str) -> tuple[str, str | None]:
    """«Георгий Кайтмазов» → («Георгий», «Кайтмазов»); одно слово → (name, None)."""
    parts = full_name.split(None, 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return full_name, None


def _multi_values(multi: Any) -> list[str]:
    """Значения мульти-поля из ответа crm.contact.get (PHONE/IM)."""
    if not isinstance(multi, list):
        return []
    return [str(v.get("VALUE")) for v in multi if isinstance(v, dict) and v.get("VALUE")]


async def main() -> None:
    token_mgr = TokenManager(
        client_id=os.environ["B24_CLIENT_ID"],
        client_secret=os.environ["B24_CLIENT_SECRET"],
    )
    token = await token_mgr.get_token()
    if token is None:
        print("ERROR: B24 token not found.")
        return
    client = Bitrix24Client(client_endpoint=os.environ["B24_PORTAL"].rstrip("/") + "/rest/")

    try:
        async with async_session() as s:
            contacts = (
                (await s.execute(select(Contact).where(Contact.crm_contact_id.is_not(None))))
                .scalars()
                .all()
            )

        for c in contacts:
            card = await client.call(
                "crm.contact.get",
                auth_token=token.access_token,
                params={"ID": c.crm_contact_id},
            )
            if not isinstance(card, dict):
                print(f"contact row {c.id}: B24 id={c.crm_contact_id} недоступен — skip")
                continue

            fields: dict[str, Any] = {}
            if c.phone and not _multi_values(card.get("PHONE")):
                fields["PHONE"] = [{"VALUE": c.phone, "VALUE_TYPE": "MOBILE"}]
            if c.username and not _multi_values(card.get("IM")):
                fields["IM"] = [{"VALUE": c.username, "VALUE_TYPE": "TELEGRAM"}]

            if not card.get("LAST_NAME"):
                if c.first_name or c.last_name:
                    first, last = c.first_name, c.last_name
                elif card.get("NAME"):
                    first, last = _split_name_fallback(str(card["NAME"]))
                else:
                    first, last = None, None
                if first and last:
                    fields["NAME"] = first
                    fields["LAST_NAME"] = last

            if not fields:
                print(f"contact row {c.id} → B24 id={c.crm_contact_id}: уже полный")
                continue
            await client.call(
                "crm.contact.update",
                auth_token=token.access_token,
                params={"ID": c.crm_contact_id, "fields": fields},
            )
            print(f"contact row {c.id} → B24 id={c.crm_contact_id}: дозаполнено {sorted(fields)}")
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
