# Plan 005: Замкнуть исходящий TG-цикл — статус/идентификатор сообщения + честный attempts

> **Executor instructions**: шаг за шагом с Verify. STOP — стоп и доклад. Обнови строку в `plans/README.md`.
>
> **Drift check**: `git diff --stat 24a661e..HEAD -- src/app/models/outbox.py src/app/bridge/outbox_repo_sqlalchemy.py src/app/bridge/outbox_repo_worker.py src/app/bridge/outbox_worker.py src/app/web/routes/dialogs.py alembic/versions/`. Расхождение = STOP.

## Status
- **Priority**: P1 | **Effort**: M | **Risk**: LOW (аддитивная колонка + два апдейта)
- **Depends on**: none (но выполняется до 006)
- **Category**: bug
- **Planned at**: commit `24a661e`, 2026-08-14

## Why this matters

Исходящее сообщение в UI висит с ⏳ вечно: `Message.status` создаётся `pending` и **никогда** не меняется; `tg_message_id` выбрасывается (комментарий в `mark_sent` врёт: «хранится в записи Message» — ничего не пишется); при провале отправки менеджер не видит ⚠. Причина: `OutboxItem` не связан с `Message`. Второй баг: `reschedule` инкрементит `attempts` и для дефералов (throttle/no_provider) → после 4 безобидных откладываний первая же реальная ошибка фейлит сообщение; а `no_provider` вообще ретраит вечно без терминального состояния. Замыкаем цикл: колонка-связка, обновление Message при sent/failed, attempts только за реальные попытки отправки.

## Current state

- `src/app/models/outbox.py` — `OutboxItem`: `id, dialog_id, tg_account_id, external_chat_id, text, attachment_id, is_initiation, status, attempts, next_attempt_at, last_error` + `created_at/updated_at` (TimestampMixin). **Нет `message_id`.**
- `src/app/web/routes/dialogs.py:146-165` (`send_message`): создаёт `Message(dialog_id, direction=outbound, status=MessageStatus.pending, author_user_id=...)` → `flush` → `repo.enqueue(dialog_id=..., tg_account_id=..., external_chat_id=..., text=..., is_initiation=...)` → commit.
- `src/app/bridge/outbox_repo_sqlalchemy.py`:
  - `enqueue(*, dialog_id, tg_account_id, external_chat_id, text, is_initiation=False)` — insert status=queued, attempts=0, flush без commit.
  - `mark_sent(item, external_message_id)` — только `update OutboxItem set status=sent` (комментарий про Message — ложь).
  - `mark_failed(item, error)` — только outbox.
  - `reschedule(item, *, delay_seconds, error=None)` — `attempts=OutboxItem.attempts + 1` в SQL (для КАЖДОГО вызова, включая дефералы), status=retrying, next_attempt_at, last_error.
- `src/app/bridge/outbox_repo_worker.py` — `WorkerOutboxRepository`: адаптер, на каждый метод свежая `async with session_factory()` → делегирует `SqlAlchemyOutboxRepository`. Подпись методов 1:1.
- `src/app/bridge/outbox_worker.py:114-157` (`_handle`): `no_provider → reschedule(30s)`; `throttled → reschedule(10s)`; успех → `mark_sent`; flood → reschedule; иначе: `if item.attempts + 1 >= max_attempts: mark_failed` else `reschedule(30*2**attempts)`.
- `src/app/models/message.py` — `MessageStatus`: pending/sent/delivered/read/error; `Message.tg_message_id`, `Message.sent_at`.
- Миграции: head см. `alembic heads` (после плана 004 — его миграция; если 004 ещё не сделан, head = `7f79d9761e13`; в любом случае down_revision = текущий head на момент старта).
- Тесты-образцы: `tests/unit/test_outbox_repo_sqlalchemy.py` (repo против SQLite), `tests/unit/test_outbox_worker.py` (worker против AsyncMock-repo).

## Commands

| Purpose | Command | Expected |
|---|---|---|
| Тесты | `.venv/Scripts/python.exe -m pytest -q` | all pass |
| Линт | `.venv/Scripts/ruff.exe check src/ tests/` | exit 0 |

## Scope

**In scope**: `src/app/models/outbox.py`, новая миграция, `src/app/bridge/outbox_repo_sqlalchemy.py`, `src/app/bridge/outbox_repo_worker.py`, `src/app/bridge/outbox_worker.py`, `src/app/web/routes/dialogs.py` (один вызов enqueue), `tests/unit/test_outbox_repo_sqlalchemy.py`, `tests/unit/test_outbox_worker.py`, `tests/integration/test_dialogs_api.py` (assert дополнить).

**Out of scope**: `status_stream`/read-receipts (delivered/read — позже; тут только pending→sent|error), outbound timeline в B24 (план 006), UI (`app.js` уже рендерит статусы — не трогаем), `IncomingHandler`.

## Git workflow
`main`, коммиты: `feat(models): outbox.message_id link + migration`, `fix(outbox): deferrals don't consume attempts; no_provider terminal`, `fix(outbox): mark_sent/mark_failed update Message status+tg_message_id`.

## Steps

### Step 1: Красные тесты

1. `tests/unit/test_outbox_repo_sqlalchemy.py` — новые:
   - `test_mark_sent_updates_message`: seed Message(out,pending) + OutboxItem(message_id=...); `mark_sent(item, external_message_id=999)` → Message.status==sent, Message.tg_message_id==999, Message.sent_at is not None; OutboxItem.status==sent.
   - `test_mark_failed_updates_message`: → Message.status==error, last_error у outbox.
   - `test_reschedule_deferral_no_attempt_increment`: `reschedule(..., delay_seconds=10, error="throttled", count_attempt=False)` → attempts НЕ изменился, status==retrying; и `count_attempt=True` (default) → attempts+1.
2. `tests/unit/test_outbox_worker.py` — новые (мок repo фиксирует kwargs):
   - `test_no_provider_not_counted_and_terminal_after_day`: item с `created_at` сутки назад → worker вызывает `mark_failed(item, "no_provider_timeout")`, НЕ reschedule; свежий item → reschedule с `count_attempt=False`.
   - `test_throttle_reschedule_not_counted`: throttled → reschedule(..., count_attempt=False).
3. `tests/integration/test_dialogs_api.py::test_send_message_creates_message_and_outbox` — добавить ассерт `outbox[0].message_id == <id нового Message>`.

**Verify**: `pytest tests/unit/test_outbox_repo_sqlalchemy.py tests/unit/test_outbox_worker.py tests/integration/test_dialogs_api.py -q` → новые красные.

### Step 2: Модель + миграция

`outbox.py`: 
```python
message_id: Mapped[int | None] = mapped_column(
    BigInteger().with_variant(Integer, "sqlite"),
    ForeignKey("messages.id"), nullable=True, index=True,
)
```
(образец варианта для SQLite — рядом в этом же файле `id`; импорт ForeignKey уже есть). Миграция руками (по образцу планов 004/фазы 2): `op.add_column("outbox", sa.Column("message_id", sa.BigInteger(), nullable=True))` + `op.create_index("ix_outbox_message_id", "outbox", ["message_id"])` + FK-констрейнт `op.create_foreign_key("fk_outbox_message", "outbox", "messages", ["message_id"], ["id"])`. Downgrade — обратное.

**Verify**: `pytest -q` — модельные/старые тесты зелёные.

### Step 3: Репозитории

`SqlAlchemyOutboxRepository`:
- `enqueue(..., message_id: int | None = None)` — прокинуть в insert.
- `mark_sent`: в той же транзакции:
```python
await self._session.execute(
    update(OutboxItem).where(OutboxItem.id == item.id)
    .values(status=OutboxStatus.sent))
if item.message_id:
    await self._session.execute(
        update(Message).where(Message.id == item.message_id)
        .values(status=MessageStatus.sent,
                tg_message_id=external_message_id,
                sent_at=func.now()))
await self._session.commit()
```
- `mark_failed`: аналогично `Message.status=error` (external id нет).
- `reschedule(..., count_attempt: bool = True)`: `attempts=OutboxItem.attempts + 1` только если count_attempt, иначе не трогать attempts.
`WorkerOutboxRepository`: прокинуть `message_id` в enqueue и `count_attempt` в reschedule (делегирование).

**Verify**: `pytest tests/unit/test_outbox_repo_sqlalchemy.py tests/unit/test_outbox_repo_worker.py -q` → зелёные.

### Step 4: Worker — дефералы не считаются; no_provider терминален

`outbox_worker.py/_handle`:
- `no_provider`: если `item.created_at` старше 24ч (`datetime.now(UTC) - item.created_at > timedelta(hours=24)`) → `mark_failed(item, "no_provider_timeout")`; иначе `reschedule(item, delay_seconds=30, error="no_provider", count_attempt=False)`.
- `throttled`: `reschedule(..., delay_seconds=10, error="throttled", count_attempt=False)`.
- Остальные reschedule (flood_wait, backoff) — `count_attempt=True` (default).
- `item.created_at` доступен (TimestampMixin); worker работает с ORM-объектом из fetch_due — ок. Импорт `timedelta, UTC`.

**Verify**: `pytest tests/unit/test_outbox_worker.py -q` → зелёные (включая 2 новых).

### Step 5: Route — прокинуть message_id

`dialogs.py/send_message`: `repo.enqueue(..., message_id=message.id)`. Полный прогон, ruff, коммиты.

**Verify**: `pytest -q` → всё green (86 + ~6 новых); `ruff check` exit 0.

## Test plan
Перечислены в Step 1 (3+2+1). Паттерны: repo-тесты — `test_outbox_repo_sqlalchemy.py`; worker-тесты — мок-repo с `call_args` (существующий файл); integration — extend существующий send-тест.

## Done criteria
- [ ] `pytest -q` green; `ruff check` exit 0
- [ ] `grep -n "message_id" src/app/models/outbox.py` и в миграции
- [ ] `grep -n "count_attempt" src/app/bridge/outbox_worker.py` — есть
- [ ] Интеграционный тест ассертит `outbox.message_id`
- [ ] Миграция применена на VM (оператор: `alembic upgrade head`)
- [ ] `plans/README.md`: 005 → DONE

## STOP conditions
- В `outbox_worker.py` появляется потребность в полях, которых нет у item (напр. нет `created_at` — значит TimestampMixin не примешан; STOP, доложи).
- Старыё тесты `test_outbox_worker.py` жёстко ассертят старые kwargs reschedule — обнови их осознанно (это ожидаемая правка, не STOP), но если их больше трёх и смысл меняется — доложи список.
- Миграция падает на VM из-за существующих данных — STOP, дамп ошибки оператору.

## Maintenance notes
- `delivered/read` статусы: следующий шаг — подписка на Telethon read-receipts (чат/диалог помечен прочитанным) → `_status_queue` → bridge-таск обновляет Message. Задел: `Message.tg_message_id` теперь заполняется — поиск по нему станет возможен.
- `no_provider_timeout` 24ч — константа в коде; при появлении админки вынести в конфиг.
- 006 (crm_sync) будет класть outbound-timeline запись именно при успехе mark_sent — kolonka message_id теперь есть.
