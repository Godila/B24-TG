#!/usr/bin/env python3
"""Одноразовая проба: резолв имён/телефонов MAX по userId (GET_CONTACTS 32)
и структура CHAT_INFO (61). Работает на сохранённой soak-сессии
(spike_max_session.json, отдельная device-сессия — прод не трогаем).
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from spike_max_login import open_saved_session

OP_GET_CONTACTS = 32
OP_CHAT_INFO = 61

# Отправитель из e2e-диалога и наш chatId.
SENDER_IDS = [349157962]
CHAT_ID = 53007183


async def main() -> None:
    client, _sess, _login = await open_saved_session()
    print("LOGIN ok", file=sys.stderr)

    for op, payload, label in [
        (OP_GET_CONTACTS, {"contactIds": SENDER_IDS}, "GET_CONTACTS by userId"),
        (OP_CHAT_INFO, {"chatId": CHAT_ID}, "CHAT_INFO"),
    ]:
        try:
            resp = await client.request(op, payload, timeout=12)
            print(f"\n=== {label} (op={op}) ===")
            print(json.dumps(resp, ensure_ascii=False, indent=2)[:4000])
        except Exception as exc:  # noqa: BLE001
            print(f"\n=== {label} (op={op}) === ERROR {type(exc).__name__}: {exc}")

    await client.close()


if __name__ == "__main__":
    asyncio.run(main())
