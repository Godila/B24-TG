#!/usr/bin/env python3
"""Спайк feed-уведомлений (Wazzup-паритет): живой прогон im-методов на портале.

Запуск внутри web-контейнера на VM (как seed_manager):
    docker compose exec web python /app/scripts/spike_im_notification.py

Проверяет три неизвестных, которые доки не фиксируют однозначно:
  1. Формат KEYBOARD для im.message.add от REST-приложения (ожидание:
     {"BUTTONS": [[{"TYPE": "link", "TEXT": ..., "LINK": ...}]]}) — кнопка
     «Отвечать не нужно» должна отрендериться в чате адресата.
  2. im.message.delete своего сообщения сразу после add.
  3. Повторный delete того же id (код «не найдено» — терпимый для деградации)
     и delete СТАРОГО сообщения (окно правки CANT_EDIT_MESSAGE).

Итог — печать кодов ошибок повторного delete (для TOLERABLE_DELETE_CODES
в crm_sync_worker.py) и вердикт по формату KEYBOARD.
"""

import asyncio
import sys

from app.b24.client import Bitrix24Client
from app.b24.im import ImService
from app.b24.token_manager import TokenManager
from app.config import get_settings


async def main() -> None:
    settings = get_settings()
    user_id = settings.alert_admin_b24_user_id
    client = Bitrix24Client(
        client_endpoint=settings.b24_portal.rstrip("/") + "/rest/",
        min_interval=settings.b24_min_call_interval,
    )
    im = ImService(client)
    token_mgr = TokenManager(
        client_id=settings.b24_client_id, client_secret=settings.b24_client_secret
    )
    token = await token_mgr.get_token()
    if token is None:
        print("FAIL: no B24 token")
        sys.exit(1)
    auth = token.access_token

    # 1. add с KEYBOARD (проверяемый формат).
    keyboard = {
        "BUTTONS": [
            [
                {"TYPE": "link", "TEXT": "Открыть диалог", "LINK": settings.b24_portal},
                {"TYPE": "link", "TEXT": "Отвечать не нужно", "LINK": settings.b24_portal},
            ]
        ]
    }
    msg_id = await im.send_notification(
        auth, user_id, "Спайк: сообщение с кнопками (будет удалено)", keyboard
    )
    # Единственный живой чек контракта (по AGENTS: спайк — один assert):
    # im.message.add с KEYBOARD возвращает id сообщения.
    assert msg_id > 0
    print(f"add+KEYBOARD ok: message_id={msg_id}")
    print(">>> Проверь глазами в чате приложения: две LINK-кнопки под сообщением.")

    # 2. delete сразу.
    await im.delete_message(auth, msg_id)
    print(f"delete ok: {msg_id}")

    # 3. повторный delete (не найдено) — какой код?
    try:
        await im.delete_message(auth, msg_id)
        print("repeat delete: ok (идемпотентен)")
    except Exception as exc:  # noqa: BLE001 — спайк печатает всё
        print(f"repeat delete error: {exc!r}")

    # 4. окно правки: попытка удалить ОДНО из старых is_new-уведомлений
    #    (осталось от прежнего поведения, упирается в CANT_EDIT_MESSAGE?).
    old_id = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    if old_id:
        try:
            await im.delete_message(auth, old_id)
            print(f"old delete ok: {old_id} (окно не истекло)")
        except Exception as exc:  # noqa: BLE001
            print(f"old delete error: {exc!r}")

    await client.aclose()
    print("SPIKE DONE")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
