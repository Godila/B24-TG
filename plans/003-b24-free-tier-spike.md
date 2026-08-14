# Plan 003: Spike — верификация B24 free-tier CRM-методов на прод-портале

> **Executor instructions**: фаза A (код + тест) — твоя; фаза B (запуск на проде) — оператора. Не импровизируй с чужим порталом: скрипт создаёт ровно один контакт/сделку и удаляет их. STOP-условия — стоп и доклад. Обнови строку в `plans/README.md`.
>
> **Drift check**: `git diff --stat 24a661e..HEAD -- scripts/ src/app/b24/`. Расхождение с «Current state» = STOP.

## Status
- **Priority**: P1 (go-live блокер) | **Effort**: S | **Risk**: LOW (скрипт чистит за собой)
- **Depends on**: none (запускать параллельно с 001/002)
- **Category**: direction / spike
- **Planned at**: commit `24a661e`, 2026-08-14

## Why this matters

Портал `b24-ye2jjz.bitrix24.ru` — **бесплатный тариф**. План Фазы 2 сам предвидел `ACCESS_DENIED: REST API is available only on commercial plans` для CRM-методов, но «зафиксировать ограничение» так и не было сделано. Весь CRM-путь (`crm.duplicate.findbycomm`, `crm.item.add`, `crm.timeline.comment.add`, `im.message.add`) ни разу не выполнялся на проде: аккаунт TG оффлайн, `process_inbound` никогда не звался, smoke Фазы 4 проверял только health/TLS/placement. Если free-tier режет любой из методов — «killer feature» (тексты сообщений в timeline сделки) молча не работает: ошибки глотаются (`incoming_handler.py:49-52`), чат живёт, CRM — нет. Это надо знать ДО подключения номера и выбора тарифа.

## Current state

- `src/app/b24/client.py` — `Bitrix24Client(client_endpoint)`, `async call(method, auth_token, params)`, кидает `Bitrix24Error(code, description)` при `error` в ответе.
- `src/app/b24/token_manager.py` — `TokenManager(client_id, client_secret)`, `async get_token() -> B24Token | None` (токены лежат в таблице `b24_tokens`, на проде импортированы — скрипт найдёт их сам).
- `src/app/b24/crm.py` — константы `ENTITY_CONTACT=3`, `ENTITY_DEAL=2`; `CrmService` (нужен только как референс параметров; скрипт может звать client напрямую).
- Существующий образец скрипта: `scripts/register_placement.py` — тот же паттерн (env → TokenManager → Bitrix24Client → вызов → печать). Скопируй структуру.
- ENV на проде: `B24_CLIENT_ID`, `B24_CLIENT_SECRET`, `B24_PORTAL` — в `/opt/bitrix-tg/.env` (запуск через `docker compose exec web`, env подхвачен).
- Удаление: метод `crm.item.delete(entityTypeId, id)` — существует (универсальный, зеркалит item.add).

## Commands

| Purpose | Command | Expected |
|---|---|---|
| Тесты | `.venv/Scripts/python.exe -m pytest -q` | all pass |
| Линт | `.venv/Scripts/ruff.exe check src/ tests/` | exit 0 |
| Локальная проверка скрипта | `.venv/Scripts/python.exe scripts/verify_b24_methods.py --help` | печатает usage, exit 0, НЕТ сетевых вызовов |

## Scope

**In scope**: новый `scripts/verify_b24_methods.py`, новый `tests/unit/test_verify_b24_methods.py`, `docs/B24-FREE-TIER.md` (создаётся оператором/адвайзером после фазы B — заготовку можешь добавить).

**Out of scope**: любые изменения `src/app/b24/*` (если spike вскроет баг в клиенте — STOP и доклад, фикс отдельным решением), изменение данных портала кроме create→delete пары contact/deal.

## Git workflow
`main`, коммит: `feat(scripts): verify_b24_methods — free-tier CRM methods spike`.

## Steps (фаза A — executor)

### Step 1: Скрипт `scripts/verify_b24_methods.py`

Структура (по образцу `register_placement.py`):

```python
#!/usr/bin/env python3
"""Spike: какие B24-методы доступны на текущем тарифе портала.

Создаёт ОДИН тестовый контакт + сделку, пишет timeline-комментарий,
шлёт IM-сообщение админу, затем удаляет за собой. Результат — таблица
OK/ERROR по каждому методу. Запуск на VM:
    docker compose exec web python /app/scripts/verify_b24_methods.py
"""
import asyncio, os, sys
from app.b24.client import Bitrix24Client, Bitrix24Error
from app.b24.token_manager import TokenManager

RESULTS: list[tuple[str, str, str]] = []  # (method, status, detail)

async def step(client, token, name, coro_factory, cleanup=None):
    try:
        result = await coro_factory()
        RESULTS.append((name, "OK", str(result)[:80]))
        return result
    except Bitrix24Error as e:
        RESULTS.append((name, "ERROR", f"{e.code}: {e.description}"[:120]))
        return None
    except Exception as e:
        RESULTS.append((name, "FATAL", repr(e)[:120])); return None
```
Последовательность шагов (каждый через `step`):
1. `app.info` (read).
2. `crm.duplicate.findbycomm` c `type=PHONE, values[]=["+79990000000"]` (read; «не найдено» — это ОК).
3. `crm.item.add` entityTypeId=3 (CONTACT), fields: `NAME="Spike Test"`, `PHONE=[{"VALUE":"+79990000001"}]` → сохранить `contact_id`.
4. `crm.item.add` entityTypeId=2 (DEAL), fields: `TITLE="SPIKE TEST — можно удалить"`, `CONTACT_ID=contact_id` → `deal_id`.
5. `crm.timeline.comment.add` на deal (`ENTITY_TYPE="deal"`, `ENTITY_ID=deal_id`, `COMMENT="spike"`).
6. `im.message.add` `DIALOG_ID=int(os.environ.get("SPIKE_ADMIN_USER_ID", "1"))`, `MESSAGE="Bitrix-TG spike: im.message.add works"`.
7. Cleanup (в `finally`-духе, отдельные step-вызовы, ошибки не маскируют результат): `crm.item.delete` deal, затем contact.

В конце: печать таблицы (formatted print), `sys.exit(0 если все OK / 1 иначе)`. Добавь `--help` через argparse с флагом `--skip-im` (не дёргать чат админа при повторных прогонах).

### Step 2: Юнит-тест без сети

`tests/unit/test_verify_b24_methods.py`: импортируй скрипт как модуль (`sys.path` трюк не нужен — scripts не пакет; вместо этого вынеси логику шагов в функцию `async def run_verification(client, token) -> list` внутри скрипта, `main()` только собирает клиент). Тест: мок `Bitrix24Client.call` (AsyncMock с side_effect по методам), вызвать `run_verification`, ассерты: 7 записей в результате; при `Bitrix24Error("ACCESS_DENIED", ...)` на шаге 3 — последующие зависимые шаги получают `ERROR: skipped-dependency` (реализуй: если `contact_id is None` → помечай и пропускай), cleanup не падает.

**Verify**: `pytest tests/unit/test_verify_b24_methods.py -q` → pass; `python scripts/verify_b24_methods.py --help` печатает usage (сетевых вызовов нет — main не запускается без токена, но --help через argparse выходит раньше).

### Step 3: Заготовка отчёта

Создай `docs/B24-FREE-TIER.md` с шаблоном: дата, коммит, таблица «метод → статус → решение» с пустыми строками и примечанием «заполнить после фазы B; решения: OK / платный тариф / деградация (какая фича отключается)».

**Verify**: файл существует, `pytest -q` green, `ruff check` exit 0. Коммит.

## Steps (фаза B — оператор, вне executor)

1. На VM: `cd /opt/bitrix-tg && git pull && docker compose up -d --build web`.
2. `docker compose exec web python /app/scripts/verify_b24_methods.py`.
3. Вывод вставить в `docs/B24-FREE-TIER.md`, заполнить колонку «решение», закоммитить.
4. Если хоть один метод ERROR/FATAL → решение: (а) платный тариф, (б) деградация продукта (какая фича отпадает), (в) обходной метод. Записать. Это вход для плана 006 (durability станет острее при ACCESS_DENIED).

## Test plan
Один юнит-тест без сети (мок клиента), покрывает: happy-путь, ACCESS_DENIED в середине, пропуск зависимых шагов, cleanup. Реальная сеть — фаза B.

## Done criteria (executor-часть)
- [ ] `scripts/verify_b24_methods.py` существует, `--help` работает, сеть не трогает
- [ ] `pytest -q` green (+1-2 теста)
- [ ] `ruff check src/ tests/` exit 0 (и scripts, если в scope ruff)
- [ ] `docs/B24-FREE-TIER.md` заготовка на месте
- [ ] `plans/README.md`: 003 → DONE (полностью DONE — только после фазы B оператора; статус «AWAITING OPERATOR» до того)

## STOP conditions
- В `b24/client.py`/`token_manager.py` сигнатуры не совпадают с описанием.
- Тест требует реальную сеть (мок не встаёт) — не ослабляй тест сетью, разберись.
- Скрипт по ходу дела вынужден создавать БОЛЬШЕ одной пары сущностей — упрости до одной, не выйдет — STOP.

## Maintenance notes
- Скрипт пригодится повторно при смене тарифа/портала — держи его идемпотентным (всегда чистит).
- Результат фазы B напрямую определяет судьбу фич: `im.message.add` недоступен → план 009 (алерты) меняет транспорт; `crm.item.add` недоступен → весь продукт на этом портале деградирует до read-only timeline — эскалация к пользователю.
