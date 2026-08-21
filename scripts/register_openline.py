#!/usr/bin/env python3
"""Регистрация коннектора открытых линий «ЧатМост» (imconnector).

- imconnector.register — коннектор появляется в Контакт-центре → Открытые
  линии → «Все каналы»; повторный вызов с тем же ID обновляет (идемпотентно).
- event.bind × 3 → /webhook/b24/imconnector: исходящие операторов
  (ONIMCONNECTORMESSAGEADD), удаление линии (…LINEDELETE), отключение
  коннектора на линии (…STATUSDELETE).

Привязка линии ЧатМост ↔ линии B24 — в слайдере коннектора (настройки
открытой линии, placement SETTING_CONNECTOR), не здесь.

ОПЕРАТОРСКАЯ ШАПКА перед запуском: в настройках локального приложения
Битрикс24 выдать права «Чаты и колл-центр (imopenlines)» и
«Коннекторы мессенджеров (imconnector)», затем переустановить приложение
(форс-рефреш scope, иначе ERROR_SCOPE). Переустановка заодно сохранит
application_token — им авторизуются вебхуки коннектора.

Запуск на VM (с доступом к .env):
    docker compose exec web python /app/scripts/register_openline.py
"""

import asyncio
import base64
import os
import sys
from pathlib import Path

from app.b24.client import Bitrix24Client, Bitrix24Error
from app.b24.openlines import CONNECTOR_ID
from app.b24.token_manager import TokenManager

BASE_URL = os.environ.get("PUBLIC_BASE_URL", "https://b24-tg.haragy.top")

# Иконка коннектора: data-uri (квадратное лого 192×192 из статик-ассетов).
_ICON_PATH = Path(__file__).resolve().parents[1] / "src" / "app" / "static" / "brand"
_ICON_FILE = "logo-square-192x192.png"


def _icon_data_uri() -> str:
    p = _ICON_PATH / _ICON_FILE
    if not p.is_file():
        print(f"WARNING: иконка не найдена: {p} — регистрация без ICON упадёт")
        return ""
    return "data:image/png;base64," + base64.b64encode(p.read_bytes()).decode("ascii")


REGISTER = {
    "ID": CONNECTOR_ID,
    "NAME": "ЧатМост",
    "ICON": {"DATA_IMAGE": _icon_data_uri(), "COLOR": "#2f6fed", "SIZE": "90%", "POSITION": "center"},
    "PLACEMENT_HANDLER": f"{BASE_URL}/placement/connector",
    # Без подписи: текст оператора приходит чистым ([b]Имя:[/b] не клеится).
    "NEED_SIGNATURE": False,
    # Личные переписки: группировка сессий по user.id (не по chat.id).
    "CHAT_GROUP": False,
    # Системные сообщения линии (диалог закрыт и пр.) — не в мессенджер.
    "NEED_SYSTEM_MESSAGES": False,
    # CRM-рассылки каналом — v2.
    "NEWSLETTER": False,
}

EVENTS = {
    "ONIMCONNECTORMESSAGEADD": f"{BASE_URL}/webhook/b24/imconnector",
    "ONIMCONNECTORLINEDELETE": f"{BASE_URL}/webhook/b24/imconnector",
    "ONIMCONNECTORSTATUSDELETE": f"{BASE_URL}/webhook/b24/imconnector",
}


async def main() -> None:
    client_id = os.environ["B24_CLIENT_ID"]
    client_secret = os.environ["B24_CLIENT_SECRET"]
    portal = os.environ["B24_PORTAL"].rstrip("/") + "/rest/"

    token_mgr = TokenManager(client_id=client_id, client_secret=client_secret)
    token = await token_mgr.get_token()
    if token is None:
        print("ERROR: B24 token not found — run app install or seed tokens first.")
        sys.exit(1)

    client = Bitrix24Client(client_endpoint=portal)
    try:
        try:
            result = await client.call(
                "imconnector.register",
                auth_token=token.access_token,
                params=REGISTER,
            )
            print(f"{CONNECTOR_ID}: register result={result}")
        except Bitrix24Error as e:
            print(
                f"ERROR: imconnector.register: {e}\n"
                "Проверьте права imopenlines+imconnector и переустановку (см. шапку)."
            )
            sys.exit(1)

        for event, handler in EVENTS.items():
            try:
                result = await client.call(
                    "event.bind",
                    auth_token=token.access_token,
                    params={"event": event, "handler": handler},
                )
                print(f"event.bind {event} -> {result}")
            except Bitrix24Error as e:
                print(f"ERROR: event.bind {event}: {e}")
                sys.exit(1)

        print(
            "\nГотово. Дальше: Контакт-центр → Открытые линии → линия → "
            "«ЧатМост» → Настроить (слайдер: привязка линии ЧатМост)."
        )
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
