# Plan 006: CRM durability — очередь crm_sync с retry, rate-limit B24, shared httpx, outbound timeline

> **Executor instructions**: это самый большой план пакета. Читай «Why» и «Design» целиком перед стартом. Шаг за шагом с Verify. STOP — стоп и доклад. Обнови строку в `plans/README.md`.
>
> **Drift check**: `git diff --stat 24a661e..HEAD -- src/app/b24/ src/app/bridge/ src/app/models/ src/app/main.py`. Расхождение с «Current state» = STOP. Примечание: если планы 004/005 уже выполнены — их изменения ожидаемы, сверяй только описанные здесь файлы/строки.

## Status
- **Priority**: P2 (P1 если план 003 выявил ограничения free-tier) | **Effort**: M-L | **Risk**: MED (перестройка порядка inbound: persist раньше CRM)
- **Depends on**: 005 (OutboxItem.message_id; общий паттерн воркера)
- **Category**: architecture / perf
- **Planned at**: commit `24a661e`, 2026-08-14

## Why this matters

Сегодня исходящие в Telegram защищены полноценной очередью (5 попыток, backoff, flood-wait), а записи в CRM — fire-and-forget: `process_inbound` вызывается до сохранения сообщения, любая ошибка (rate-limit бесплатного портала ~2 rps, ACCESS_DENIED, сеть) глотается, и контакт/сделка/timeline-комментарий теряются **навсегда** — при этом каждый входящий делает 4–6 REST-вызовов без троттлинга. Плюс: исходящие сообщения вообще не пишутся в timeline сделки (обещано спекой §8.2 шаг 6), у существующих контактов сделка ищется единожды и `deal_id=None` навсегда, и каждый B24-вызов открывает новый TLS-коннект. План вводит вторую очередь (`crm_sync`) с тем же retry-механизмом, глобальный rate-limiter в клиенте, переиспользование httpx-коннектов и закрывает outbound-timeline.

## Design (что строим)

1. **Таблица `crm_sync`** (новая): `id, kind('inbound'|'outbound'), message_id FK messages.id, status(queued|done|failed), attempts, next_attempt_at, last_error` + timestamps. Один активный воркер в bridge-процессе (по образцу OutboxWorker: poll → throttle → попытка → backoff).
2. **Inbound переставляется местами**: `IncomingHandler` теперь (а) persist-ит сообщение/контакт/диалог БЕЗ crm-полей (это уже так после фазы 5 — сохраняется), (б) кладёт `crm_sync(kind=inbound, message_id=...)`; CRM-воркер позже вызывает `Bitrix24Sync.process_inbound_from_db(message_id)` и дописывает `Message.timeline_comment_id`, `Contact.crm_contact_id`, `Dialog.crm_deal_id` по результату. Уведомление менеджеру (im.message.add) — тоже из воркера, только для `is_new`-диалогов (см. Step 6).
3. **`process_inbound` становится идемпотентным**: если контакт найден — ищем его ОТКРЫТУЮ сделку (`crm.item.list entityTypeId=2 filter CONTACT_ID=contact_id, порядок по id desc, limit 1`); нет — создаём (только для is_new). Это чинит «existing contact → deal_id=None навсегда».
4. **`process_outbound(message)`** (новый в sync): timeline-комментарий по `dialog.crm_deal_id` (или contact-карточке) + сохранение `timeline_comment_id`. Воркер кладёт `crm_sync(kind=outbound)` после успешной TG-отправки (hook в `mark_sent`-путь воркера outbox: после `mark_sent` с `item.message_id`).
5. **Rate-limit + retry в `Bitrix24Client`**: глобальный (на процесс) троттлер — минимальный интервал между вызовами (default 0.6с, конфиг `B24_MIN_CALL_INTERVAL`), плюс 1 повтор при `QUERY_LIMIT_EXCEEDED` с паузой 1.5с. Общий `httpx.AsyncClient` на инстанс (создаётся в `__init__`, `aclose()` в shutdown).
6. **notify_manager** — только на первое сообщение нового диалога (сейчас — на каждое; спам + лишняя квота).

## Current state

- `src/app/bridge/incoming_handler.py:41-57` — `process_inbound(...)` вызывается ДО `_persist`, try/except глотает всё; sync_result применяется к contact/dialog/message при persist.
- `src/app/b24/sync.py:41-107` — `process_inbound(sender_name, sender_phone, message_text, assigned_b24_user_id) -> SyncResult|None`: find_contact_by_phone → (new: create_contact+create_deal) → add_timeline_comment(deal|contact) → notify_manager (безусловно). `SyncResult(contact_id, deal_id, is_new, timeline_comment_id)`.
- `src/app/b24/client.py` — `call()` создаёт `httpx.AsyncClient` в `async with` на каждый вызов; ошибок rate-limit не различает.
- `src/app/b24/crm.py` — `find_contact_by_phone`, `create_contact`, `create_deal`, `add_timeline_comment`. Метода «найти сделку контакта» нет.
- `src/app/main.py:run_bridge()` — после фазы 5: строит sm/handler/outbox worker/health/forward; здесь добавить CrmSyncWorker и httpx-aclose.
- `src/app/models/__init__.py` — реэкспорт всех моделей (порядок алфавитный).
- Миграции — hand-written (образец: план 004/005, `alembic heads` = актуальный head).
- Тесты-образцы: worker-моки — `tests/unit/test_outbox_worker.py`; sync-моки — `tests/unit/test_b24_sync.py`; handler против БД — `tests/unit/test_incoming_handler_db.py` (из плана 004).

## Commands

| Purpose | Command | Expected |
|---|---|---|
| Тесты | `.venv/Scripts/python.exe -m pytest -q` | all pass |
| Линт | `.venv/Scripts/ruff.exe check src/ tests/` | exit 0 |

## Scope

**In scope**: новая модель+миграция `crm_sync`; `src/app/b24/client.py` (троттлер, retry, shared client, `aclose`); `src/app/b24/sync.py` (идемпотентность, `process_outbound`, изъятие notify в параметр); `src/app/b24/crm.py` (+`find_open_deal_for_contact`); `src/app/bridge/incoming_handler.py` (persist-then-enqueue); новый `src/app/bridge/crm_sync_worker.py` (+repo-слой по образцу outbox_repo_worker); `src/app/bridge/outbox_worker.py` (hook после mark_sent); `src/app/main.py` (запуск воркера, aclose); `src/app/config.py` (+`b24_min_call_interval`); соотв. тесты.

**Out of scope**: UI, web-роуты, `IncomingMessage`/провайдер, HealthChecker, любые изменения outbox-механики кроме hook-точки, Sentry/алерты (план 009), WebSocket.

## Git workflow
`main`, атомарные коммиты: `feat(b24): throttled shared-client Bitrix24Client`, `feat(models): crm_sync queue + migration`, `feat(bridge): CrmSyncWorker`, `refactor(inbound): persist first, CRM via queue`, `feat(b24): process_outbound + idempotent inbound`.

## Steps

### Step 1: `Bitrix24Client` — shared client + троттлер + retry

1. `__init__`: `self._http = httpx.AsyncClient(timeout=self._timeout)`; `self._last_call = 0.0`; `self._min_interval = min_interval` (новый kwarg, default 0.6).
2. В `call()`: перед запросом — `asyncio.Lock`-защищённое ожидание интервала:
```python
async with self._interval_lock:
    wait = self._last_call + self._min_interval - time.monotonic()
    if wait > 0: await asyncio.sleep(wait)
    self._last_call = time.monotonic()
```
3. Ответ с `error == "QUERY_LIMIT_EXCEEDED"` (это тело при HTTP 200): один повтор после `asyncio.sleep(1.5)`; второй раз — обычный `Bitrix24Error`.
4. `aclose()`: закрыть `self._http`. Убрать `async with` — использовать `self._http.request`.
5. Тесты (`tests/unit/test_b24_client.py`): интервал соблюдается между двумя вызовами (mock времени не нужен — патч `asyncio.sleep` и проверь, что sleep вызван с >0 при быстром втором вызове); QUERY_LIMIT_EXCEEDED → повтор → успех; aclose закрывает клиент (мок `httpx.AsyncClient`).

**Verify**: `pytest tests/unit/test_b24_client.py -q` → pass.

### Step 2: Модель `crm_sync` + миграция

Модель `src/app/models/crm_sync.py` (по стилю `outbox.py`): колонки выше; enum `CrmSyncStatus(queued/done/failed)`; добавить в `models/__init__.py` (алфавит). Миграция hand-written: create_table + индексы (message_id, status+next_attempt_at).

**Verify**: `pytest -q` — модель подхватилась, ничего не сломалось.

### Step 3: `CrmService.find_open_deal_for_contact` + идемпотентный `process_inbound`

1. `crm.py`: 
```python
async def find_open_deal_for_contact(self, auth_token, contact_id) -> DealInfo | None:
    result = await self._client.call("crm.item.list", auth_token=auth_token,
        params={"entityTypeId": ENTITY_DEAL,
                "filter": {"CONTACT_ID": contact_id, "CLOSED": "N"},
                "order": {"id": "desc"}, "select[]": ["id", "title"]})
    items = result.get("items", []) if isinstance(result, dict) else []
    if not items: return None
    it = items[0]
    return DealInfo(id=int(it["id"]), title=it.get("title"))
```
2. `sync.py/process_inbound`: в ветке существующего контакта — `deal = await crm.find_open_deal_for_contact(...)`; если нет — только при `is_new` создаём (у существующего контакта без сделок — комментируем в contact-карточку, как сейчас, deal_id=None). `notify_manager` вызывать ТОЛЬКО если `is_new` (новый контакт+сделка) — убрать безусловный вызов. Тесты: обнови `tests/unit/test_b24_sync.py` (existing-ветка теперь зовёт find_open_deal; notify только в new-ветке).

**Verify**: `pytest tests/unit/test_b24_crm.py tests/unit/test_b24_sync.py -q` → pass.

### Step 4: `process_outbound` в sync + входные данные из БД

1. `sync.py` новый метод:
```python
async def process_outbound(self, message) -> int | None:
    """Timeline-комментарий для исходящего message (dialog.crm_deal_id или contact)."""
```
— берёт token; определяет entity по `message.dialog.crm_entity_type/crm_deal_id` (диалог догрузить: `await session...` — НЕТ, воркер передаст готовый dict/dataclass: сделай сигнатуру `process_outbound(dialog_deal_id, dialog_entity_type, contact_id, text) -> int|None`, без ORM в b24-слое). Комментарий с префиксом `💬 Исходящее (менеджер): ` + текст. Возвращает comment_id.
2. `process_inbound_from_db` НЕ вводить отдельным методом — CrmSyncWorker (Step 5) сам соберёт параметры из Message+Dialog+Contact (select) и позовёт `process_inbound(...)`, потом применит SyncResult к строкам (update'ы: Message.timeline_comment_id, Contact.crm_contact_id, Dialog.crm_deal_id/crm_entity_type).

**Verify**: юнит-тест `process_outbound` (моки crm): deal-ветка и contact-ветка.

### Step 5: `CrmSyncWorker` + repo

Новый `src/app/bridge/crm_sync_worker.py` (структура — копия OutboxWorker в миниатюре): repo (абстракция в том же файле: `fetch_due`, `mark_done`, `mark_failed`, `reschedule`, `enqueue`) + `SqlAlchemy`-реализация внутри `outbox_repo_worker`-стиля (свежая сессия на вызов; можно положить в `src/app/bridge/crm_sync_repo.py`). Воркер: poll (интервал 2с), batch 20, для каждого item:
- `kind=inbound`: собрать данные (SELECT Message+Dialog+Contact by message_id) → `process_inbound(...)` → применить SyncResult-обновления → `mark_done`. Exception → attempts+1, backoff `30*2**attempts`, после 5 — `mark_failed` (лог ERROR).
- `kind=outbound`: собрать (Message+Dialog) → `process_outbound(...)` → `Message.timeline_comment_id=...` → mark_done.
Конфиг: max_attempts=5 (переиспользуй `outbox_max_attempts`? нет — свой `crm_sync_max_attempts: int = Field(5)` + `crm_sync_poll_interval: int = Field(2)` в config.py).
Тесты: мок-repo + мок-sync (успех/фейл/backoff/terminal) — по образцу `test_outbox_worker.py`.

**Verify**: `pytest tests/unit/test_crm_sync_worker.py -q` → pass.

### Step 6: Перестройка IncomingHandler + hook из outbox + wiring

1. `incoming_handler.py`: удалить прямой вызов `self._b24sync.process_inbound` (и применение sync_result при persist — persist пишет только crm-поля, которых нет). Вместо этого после успешного persist-коммита — `await crm_sync_repo.enqueue(kind="inbound", message_id=message.id)` (repo передаётся конструктором; зависимость `b24sync` из конструктора УБРАТЬ). `IncomingHandler(session_mgr, crm_sync_enqueue, db_session_factory)` — обновить `main.py` wiring и ВСЕ тесты handler-а (`test_incoming_handler*.py`: мок вместо b24sync — enqueue-AsyncMock; ассерты: enqueue вызван с message_id).
2. `outbox_worker.py`: после успешного `mark_sent` (ветка `result.success`), если `item.message_id` — `await self._on_sent_hook(item.message_id)`; hook — колбэк в конструкторе (default None; bridge передаёт `lambda mid: crm_sync_repo.enqueue(kind="outbound", message_id=mid)`). Тест: hook вызван.
3. `main.py/run_bridge`: построить crm_sync repo+worker, прокинуть enqueue в IncomingHandler и hook в OutboxWorker, `asyncio.create_task(crm_worker.run())`, в finally — `crm_worker.stop()` + `await b24_client.aclose()` (обрати внимание: в run_bridge два инстанса Bitrix24Client — crm и im; закрой оба, или создай один и передавай в оба сервиса — ЛУЧШЕ один: `b24_client = Bitrix24Client(...); crm = CrmService(b24_client); im = ImService(b24_client)`).
4. `test_startup.py` — обновить (новые моки).

**Verify**: `pytest -q` — весь suite green; `ruff check` exit 0.

### Step 7: Деплой (оператор)
`git pull && docker compose up -d --build && docker compose exec web alembic upgrade head && docker compose restart nginx`. Наблюдение: в логах bridge — crm_sync-воркер стартовал; ошибки B24 теперь ретраятся (логи `crm_sync retry`).

## Test plan
Перечислены по шагам: client (3), crm/sync (2), crm_sync_worker (4), handler-перестройка (обновление ~4 существующих), outbox hook (1), startup (обновление). Интеграционный smoke вручную после подключения номера (оператор).

## Done criteria
- [ ] `pytest -q` green; `ruff check src/ tests/` exit 0
- [ ] `grep -rn "process_inbound" src/app/bridge/incoming_handler.py` — пусто (прямой вызов убран)
- [ ] `grep -n "crm_sync" src/app/main.py` — wiring есть
- [ ] Миграция crm_sync применена на VM
- [ ] `plans/README.md`: 006 → DONE

## STOP conditions
- Идемпотентность `process_inbound` требует больше 2 новых B24-вызовов на ретрай (не укладываешься в квоту) — доложи дизайн-конфликт.
- Перестройка handler ломает >5 существующих тестов непонятным образом — STOP с списком.
- `crm.item.list` с filter CONTACT_ID недоступен на портале (см. результат плана 003!) — обходи через `crm.deal.list` (deprecated но живой) и зафиксируй выбор комментарием; если и он недоступен — STOP.

## Maintenance notes
- Две очереди (outbox→TG, crm_sync→B24) теперь симметричны; новые B24-фичи (например, обновление контакта) обязаны идти через crm_sync, не напрямую из handler-ов.
- Троттлер per-process: web-процесс (placement token check) не троттлится вместе с bridge — осознанно (редкие вызовы); при росте — вынести в Redis… которого нет (см. 008 — удаление; если когда-нибудь вернём — это первый кандидат).
- `tenacity` не понадобился (ручной retry) — план 008 удалит зависимость.
- Deal-search `CLOSED="N"`: если менеджеры закрывают сделки, новые сообщения создадут новую сделку для старого контакта — это поведение согласовать с CRM-менеджером (записать в docs/B24-FREE-TIER.md примечание при фазе B плана 003).
