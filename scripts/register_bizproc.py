#!/usr/bin/env python3
"""Регистрация активити БП и РОБОТА «ЧатМост: отправить сообщение».

Два реестра Битрикс24, оба на один хендлер /webhook/b24/bizproc:
- bizproc.activity.add — ДЕЙСТВИЕ для конструктора бизнес-процессов
  (Автоматизация → Бизнес-процессы, палитра «Действия приложений»);
- bizproc.robot.add — РОБОТ для визуального редактора роботов на стадиях
  воронок сделок и лидов (куда обычно смотрят менеджеры).

Шаг: сообщение уходит в последний диалог сделки/лида/контакта;
[https://ссылка] в тексте — файлом (вложением).

ОПЕРАТОРСКИЙ ШАПКА перед запуском: в настройках локального приложения
Битрикс24 выдать право «Бизнес-процессы (bizproc)» и переустановить
приложение — иначе bizproc.activity.add ответит ERROR_SCOPE.

Идемпотентно: bizproc.activity.list → CODE уже наш с тем же HANDLER — ок;
ERROR_ACTIVITY_ALREADY_INSTALLED — ок. Обновление описания/свойств
установленного активити = delete + add (v1 не делаем — подсказка ниже).

Запуск на VM (с доступом к .env):
    docker compose exec web python /app/scripts/register_bizproc.py
"""

import asyncio
import os

from app.b24.client import Bitrix24Client, Bitrix24Error
from app.b24.token_manager import TokenManager

BASE_URL = "https://b24-tg.haragy.top"

ACTIVITY = {
    "CODE": "chatmost_send_message",
    "HANDLER": f"{BASE_URL}/webhook/b24/bizproc",
    # N — fire-and-forget: 200 хендлера завершает шаг, доставку тянет outbox.
    "USE_SUBSCRIPTION": "N",
    "NAME": {"ru": "ЧатМост: отправить сообщение", "en": "ChatMost: send message"},
    "DESCRIPTION": {
        "ru": "Отправляет сообщение клиенту в последний диалог этой сделки/лида/"
        "контакта. Чтобы отправить файл, вставьте ссылку на него в квадратных "
        "скобках — [https://…]: клиент получит файл, а не ссылку. Нет диалога — "
        "шаг завершится ошибкой.",
        "en": "Sends a message to the client's latest chat of this deal/lead/"
        "contact. To send a file, wrap its URL in square brackets — "
        "[https://…] — the client receives a file, not a link.",
    },
    "PROPERTIES": {
        "message": {
            "Name": {"ru": "Сообщение", "en": "Message"},
            "Type": "text",
            "Required": "Y",
        },
    },
    # Только CRM-контекст: в списках/дисках шагу делать нечего (нет диалогов).
    "FILTER": {"INCLUDE": [["crm"]]},
}

# Робот (визуальный редактор на стадиях): только сущности с воронками —
# сделки и лиды (у контактов роботов нет). CODE отличен от активити:
# пространства кодов у activity/robot общие (ERROR_ACTIVITY_ALREADY_INSTALLED).
ROBOT = {
    **ACTIVITY,
    "CODE": "chatmost_send_message_robot",
    # INCLUDE-правила робота адресуются парой [модуль, сущность документа].
    "FILTER": {"INCLUDE": [["crm", "CCrmDocumentDeal"], ["crm", "CCrmDocumentLead"]]},
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
        installed = False
        try:
            for act in (
                await client.call(
                    "bizproc.activity.list",
                    auth_token=token.access_token,
                )
                or []
            ):
                if act.get("CODE") == ACTIVITY["CODE"]:
                    installed = True
                    if act.get("HANDLER") == ACTIVITY["HANDLER"]:
                        print(f"{ACTIVITY['CODE']}: уже установлен — ок")
                    else:
                        print(
                            f"{ACTIVITY['CODE']}: установлен с ДРУГИМ handler'ом "
                            f"({act.get('HANDLER')}) — разберитесь руками"
                        )
                    break
        except Exception as e:  # noqa: BLE001 - one-off ops-скрипт
            print(f"bizproc.activity.list error (продолжаем вслепую): {e}")

        if not installed:
            try:
                result = await client.call(
                    "bizproc.activity.add",
                    auth_token=token.access_token,
                    params=ACTIVITY,
                )
                print(f"{ACTIVITY['CODE']}: add result={result}")
            except Bitrix24Error as e:
                if e.code == "ERROR_ACTIVITY_ALREADY_INSTALLED":
                    print(f"{ACTIVITY['CODE']}: уже установлен — ок")
                else:
                    raise

        # Робот для визуального редактора на стадиях (сделки/лиды).
        try:
            result = await client.call(
                "bizproc.robot.add",
                auth_token=token.access_token,
                params=ROBOT,
            )
            print(f"{ROBOT['CODE']}: robot.add result={result}")
        except Bitrix24Error as e:
            if e.code == "ERROR_ACTIVITY_ALREADY_INSTALLED":
                print(f"{ROBOT['CODE']}: робот уже установлен — ок")
            else:
                raise
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
