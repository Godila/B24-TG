#!/usr/bin/env python3
"""Spike: какие B24-методы доступны на текущем тарифе портала.

Создаёт ОДИН тестовый контакт + сделку, пишет timeline-комментарий,
шлёт IM-сообщение админу, затем удаляет за собой. Результат — таблица
OK/ERROR по каждому методу. Запуск на VM:
    docker compose exec web python /app/scripts/verify_b24_methods.py
Повторные прогоны без пинга админа:
    docker compose exec web python /app/scripts/verify_b24_methods.py --skip-im
"""
import argparse
import asyncio
import os
import sys
from collections.abc import Awaitable, Callable
from typing import Any

from app.b24.client import Bitrix24Client, Bitrix24Error
from app.b24.crm import ENTITY_CONTACT, ENTITY_DEAL
from app.b24.token_manager import TokenManager

# Тестовые данные спайка: ровно один контакт + одна сделка, удаляются в конце.
SPIKE_SEARCH_PHONE = "+79990000000"  # для findbycomm (read-only)
CONTACT_NAME = "Spike Test"
CONTACT_PHONE = "+79990000001"
DEAL_TITLE = "SPIKE TEST — можно удалить"
TIMELINE_COMMENT = "spike"
IM_MESSAGE = "Bitrix-TG spike: im.message.add works"


def extract_item_id(result: Any) -> int | None:
    """Извлечь id созданного элемента из ответа crm.item.add.

    Реальная форма ответа — ``{"item": {"id": 123, ...}}``; на случай
    plain-dict/int-ответа обрабатываем и их.
    """
    if isinstance(result, int):
        return result or None
    if isinstance(result, dict):
        item = result.get("item", result)
        if isinstance(item, dict):
            raw = item.get("id")
            return int(raw) if raw else None
    return None


def all_ok(results: list[tuple[str, str, str]]) -> bool:
    """True, если каждый шаг завершился OK (логика exit-кода)."""
    return all(status == "OK" for _, status, _ in results)


async def run_verification(
    client: Bitrix24Client, token: str, skip_im: bool = False
) -> list[tuple[str, str, str]]:
    """Прогнать все шаги спайка, вернуть список (метод, статус, деталь).

    Сетевая логика целиком здесь; ``main()`` только собирает реальный клиент.
    Сбои зависимых шагей не маскируют друг друга: каждый шаг пишет свою
    строку в таблицу, а очистка выполняется всегда, что создано.
    """
    results: list[tuple[str, str, str]] = []

    async def step(name: str, coro_factory: Callable[[], Awaitable[Any]]) -> Any:
        try:
            result = await coro_factory()
        except Bitrix24Error as e:
            results.append((name, "ERROR", f"{e.code}: {e.description}"[:120]))
            return None
        except Exception as e:  # spike: ловим всё, чтобы не потерять таблицу
            results.append((name, "FATAL", repr(e)[:120]))
            return None
        results.append((name, "OK", str(result)[:80]))
        return result

    def skipped(name: str) -> None:
        results.append((name, "ERROR", "skipped-dependency"))

    # 1. Базовый read: токен жив, порт отвечает.
    await step("app.info", lambda: client.call("app.info", auth_token=token))

    # 2. Read-only поиск дублей по телефону («не найдено» — тоже OK).
    await step(
        "crm.duplicate.findbycomm",
        lambda: client.call(
            "crm.duplicate.findbycomm",
            auth_token=token,
            params={"type": "PHONE", "values[]": [SPIKE_SEARCH_PHONE]},
        ),
    )

    # 3. Контакт: NAME + PHONE; дальше все шаги зависят от contact_id.
    contact_result = await step(
        "crm.item.add (contact)",
        lambda: client.call(
            "crm.item.add",
            auth_token=token,
            params={
                "entityTypeId": ENTITY_CONTACT,
                "fields": {"NAME": CONTACT_NAME, "PHONE": [{"VALUE": CONTACT_PHONE}]},
            },
        ),
    )
    contact_id = extract_item_id(contact_result)

    # 4. Сделка, привязанная к контакту.
    if contact_id is None:
        skipped("crm.item.add (deal)")
        deal_id = None
    else:
        deal_id = extract_item_id(
            await step(
                "crm.item.add (deal)",
                lambda: client.call(
                    "crm.item.add",
                    auth_token=token,
                    params={
                        "entityTypeId": ENTITY_DEAL,
                        "fields": {"TITLE": DEAL_TITLE, "CONTACT_ID": contact_id},
                    },
                ),
            )
        )

    # 5. Timeline-комментарий на сделке.
    if deal_id is None:
        skipped("crm.timeline.comment.add")
    else:
        await step(
            "crm.timeline.comment.add",
            lambda: client.call(
                "crm.timeline.comment.add",
                auth_token=token,
                params={
                    "fields": {
                        "ENTITY_TYPE": "deal",
                        "ENTITY_ID": deal_id,
                        "COMMENT": TIMELINE_COMMENT,
                    }
                },
            ),
        )

    # 6. IM-сообщение админу (DIALOG_ID = user_id; можно отключить --skip-im).
    if not skip_im:
        async def send_im() -> Any:
            dialog_id = int(os.environ.get("SPIKE_ADMIN_USER_ID", "1"))
            return await client.call(
                "im.message.add",
                auth_token=token,
                params={"DIALOG_ID": dialog_id, "MESSAGE": IM_MESSAGE},
            )

        await step("im.message.add", send_im)

    # 7. Очистка: сначала сделка, затем контакт. Ошибки не маскируют таблицу.
    if deal_id is None:
        skipped("crm.item.delete (deal, cleanup)")
    else:
        await step(
            "crm.item.delete (deal, cleanup)",
            lambda: client.call(
                "crm.item.delete",
                auth_token=token,
                params={"entityTypeId": ENTITY_DEAL, "id": deal_id},
            ),
        )
    if contact_id is None:
        skipped("crm.item.delete (contact, cleanup)")
    else:
        await step(
            "crm.item.delete (contact, cleanup)",
            lambda: client.call(
                "crm.item.delete",
                auth_token=token,
                params={"entityTypeId": ENTITY_CONTACT, "id": contact_id},
            ),
        )

    return results


def print_table(results: list[tuple[str, str, str]]) -> None:
    """Напечатать итоговую таблицу «метод → статус → деталь»."""
    width = max(len(name) for name, _, _ in results)
    print(f"\n{'Метод':<{width}}  СТАТУС  Детали")
    print("-" * (width + 50))
    for name, status, detail in results:
        print(f"{name:<{width}}  {status:<6}  {detail}")


async def main(skip_im: bool = False) -> int:
    """Собрать реальный клиент из env/БД и прогнать спайк. Возвращает exit-code."""
    client_id = os.environ["B24_CLIENT_ID"]
    client_secret = os.environ["B24_CLIENT_SECRET"]
    portal = os.environ["B24_PORTAL"].rstrip("/") + "/rest/"

    token_mgr = TokenManager(client_id=client_id, client_secret=client_secret)
    token = await token_mgr.get_token()
    if token is None:
        print("ERROR: B24 token not found — run app install or seed tokens first.")
        return 2

    client = Bitrix24Client(client_endpoint=portal)
    results = await run_verification(client, token.access_token, skip_im=skip_im)
    print_table(results)
    return 0 if all_ok(results) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=(
            "Spike-проверка доступности B24 CRM-методов на текущем тарифе: "
            "создаёт один контакт + сделку и удаляет их за собой."
        )
    )
    parser.add_argument(
        "--skip-im",
        action="store_true",
        help="не отправлять im.message.add админу (для повторных прогонов)",
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(main(skip_im=args.skip_im)))
