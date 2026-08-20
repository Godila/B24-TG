#!/usr/bin/env python3
"""Регистрация placement-виджетов «ЧатМост» в Bitrix24 (один раз при установке).

Биндит точки:
- CRM_DEAL_DETAIL_TAB и CRM_LEAD_DETAIL_TAB → /placement/deal — вкладка
  «ЧатМост» в карточках сделки и лида (тип сущности хендлер узнаёт из
  кода placement);
- LEFT_MENU → /placement/admin — пункт «ЧатМост» в главном меню портала
  (панель управления внутри интерфейса B24).

Идемпотентно и «самообновляемо»: placement.get → если точка не привязана,
биндим; привязана к нашему handler'у с устаревшим TITLE — unbind+bind
(заголовок иначе не обновить); привязана к чужому handler'у — предупреждаем.

Запуск на VM (с доступом к .env):
    docker compose exec web python /app/scripts/register_placement.py
"""

import asyncio
import os

from app.b24.client import Bitrix24Client
from app.b24.token_manager import TokenManager

BASE_URL = "https://b24-tg.haragy.top"

BINDINGS = [
    {
        "PLACEMENT": "CRM_DEAL_DETAIL_TAB",
        "HANDLER": f"{BASE_URL}/placement/deal",
        "TITLE": "ЧатМост",
        "LANG_ALL": {
            "ru": {"TITLE": "ЧатМост"},
            "en": {"TITLE": "ChatMost"},
        },
    },
    {
        "PLACEMENT": "CRM_LEAD_DETAIL_TAB",
        "HANDLER": f"{BASE_URL}/placement/deal",
        "TITLE": "ЧатМост",
        "LANG_ALL": {
            "ru": {"TITLE": "ЧатМост"},
            "en": {"TITLE": "ChatMost"},
        },
    },
    {
        "PLACEMENT": "LEFT_MENU",
        "HANDLER": f"{BASE_URL}/placement/admin",
        "TITLE": "ЧатМост",
        "LANG_ALL": {
            "ru": {"TITLE": "ЧатМост"},
            "en": {"TITLE": "ChatMost"},
        },
    },
]


async def main():
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
        # placement → список (handler, title). Повторный bind той же точки
        # создаёт ДУБЛИКАТ (две вкладки/два пункта) — поэтому сначала
        # сверяемся. Список, а не dict: LEFT_MENU-биндингов может быть два
        # («ЧатМост»-админка и «Чаты» — см. bind_chats_placement.py).
        existing: dict[str, list[tuple[str, str]]] = {}
        try:
            for b in (
                await client.call(
                    "placement.get",
                    auth_token=token.access_token,
                )
                or []
            ):
                existing.setdefault(str(b.get("placement", "")), []).append(
                    (str(b.get("handler", "")), str(b.get("title", ""))),
                )
        except Exception as e:  # noqa: BLE001 - one-off ops-скрипт
            print(f"placement.get error (продолжаем вслепую): {e}")

        for binding in BINDINGS:
            code = binding["PLACEMENT"]
            handler = binding["HANDLER"]
            bound = existing.get(code, [])
            mine = [title for h, title in bound if h == handler]
            foreign = [h for h, _t in bound if h != handler]
            if mine:
                if mine[0] == binding["TITLE"]:
                    print(f"{code}: уже привязан ({mine[0]!r}) — ок")
                    continue
                # Наш handler, но устаревший заголовок → unbind + bind.
                try:
                    await client.call(
                        "placement.unbind",
                        auth_token=token.access_token,
                        params={"PLACEMENT": code, "HANDLER": handler},
                    )
                    print(f"{code}: отвязан устаревший ({mine[0]!r})")
                except Exception as e:  # noqa: BLE001
                    print(f"{code}: unbind error: {e} — пробуем bind всё равно")
            elif foreign:
                print(
                    f"{code}: привязан к ДРУГОМУ handler'у "
                    f"({foreign[0]}) — пропускаем, разбирайтесь руками"
                )
                continue
            try:
                result = await client.call(
                    "placement.bind",
                    auth_token=token.access_token,
                    params=binding,
                )
                print(f"{code}: bind result={result} title={binding['TITLE']!r}")
            except Exception as e:  # noqa: BLE001 - one-off ops-скрипт
                print(f"{code}: bind error: {e}")
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
