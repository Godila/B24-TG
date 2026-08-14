# Plan 004: Мультиаккаунт — scoping диалогов по менеджеру + unique constraint

> **Executor instructions**: шаг за шагом с Verify. STOP — стоп и доклад. Обнови строку в `plans/README.md`.
>
> **Drift check**: `git diff --stat 24a661e..HEAD -- src/app/models/dialog.py src/app/bridge/incoming_handler.py alembic/versions/ tests/unit/test_incoming_handler.py`. Расхождение = STOP.

## Status
- **Priority**: P1 | **Effort**: M | **Risk**: MED (миграция с dedup; смена ключа upsert)
- **Depends on**: 002 (тесты inbound из 002 — базис; формально код независим, но прогонять вместе)
- **Category**: bug + migration
- **Planned at**: commit `24a661e`, 2026-08-14

## Why this matters

Мультиаккаунтность — главное требование заказчика (1 менеджер = 1 TG-симка = 1 b24_user_id). Сейчас она сломана: `IncomingHandler._persist` ищет диалог по `external_chat_id` **один по всему миру**, без привязки к менеджеру, и в БД нет unique-констрейнта. Для приватных TG-чатов `chat_id` = user-id клиента и **одинаков во всех аккаунтах менеджеров**. Последствия: (а) клиент, написавший менеджеру Б, прикрепляется к диалогу менеджера А (Б видит уведомление в B24, но не видит диалог в виджете; crm_deal_id А перезатирается сделкой Б); (б) гонка двух первых сообщений → две записи с одним `external_chat_id` → дальше каждый inbound ловит `MultipleResultsFound` и чат умирает навсегда; (в) полный scan dialogs на каждое сообщение (нет индекса).

## Current state

- `src/app/models/dialog.py:35` — `external_chat_id: Mapped[str] = mapped_column(String(128), nullable=False)` — без unique, без index.
- `src/app/bridge/incoming_handler.py` (после фазы 5): `handle()` → `_persist(msg, result, manager_id=account.manager_id)`; в `_persist`:
```python
# :86-98
existing_dialog = await session.execute(
    select(Dialog).where(Dialog.external_chat_id == msg.external_chat_id)
)
dialog = existing_dialog.scalar_one_or_none()
if dialog is None:
    dialog = Dialog(
        contact_id=contact.id,
        messenger=Messenger.tg,
        external_chat_id=msg.external_chat_id,
        assigned_user_id=manager_id,
    )
    session.add(dialog); await session.flush()
```
- Чтения: `web/routes/dialogs.py:67-75,87` — `Dialog.assigned_user_id == manager.id` (уже менеджер-скоуп).
- Миграции: `alembic/versions/93cd8044d35d_initial_schema.py` (все таблицы, hand-written стиль `op.create_table` + `op.f(...)` индексы), `7f79d9761e13_add_b24_tokens_table.py` (head, образец ручной миграции). Autogenerate на dev-машине НЕ работает (нет postgres) — пишем миграцию руками по образцу `7f79d9761e13`.
- Тесты: `tests/unit/test_incoming_handler.py` — мок-сессия (AsyncMock); НЕ годится для этого плана. Образец настоящей БД в тестах: `tests/integration/test_dialogs_api.py` (in-memory SQLite + StaticPool + async_sessionmaker + dependency_overrides). Для юнита репо-уровня: `tests/unit/test_outbox_repo_sqlalchemy.py` fixture `session`.
- Contact: `tg_user_id` уже **unique** (`contact.py:18-20`) — там аналогичной проблемы нет.

## Commands

| Purpose | Command | Expected |
|---|---|---|
| Тесты | `.venv/Scripts/python.exe -m pytest -q` | all pass |
| Линт | `.venv/Scripts/ruff.exe check src/ tests/` | exit 0 |
| Миграция локально | — | autogenerate недоступен (нет postgres); проверка — тестом SQLite create_all + (оператор) на VM |

## Scope

**In scope**: `src/app/models/dialog.py`, `src/app/bridge/incoming_handler.py`, новая миграция `alembic/versions/<rev>_dialog_unique_per_manager.py`, `tests/unit/test_incoming_handler_db.py` (новый), `tests/unit/test_incoming_handler.py` (только если сломается моками).

**Out of scope**: `web/routes/dialogs.py` (чтения уже корректны), `Contact`/`Message` логика, web-сокет/статусы, MAX-messenger обобщение (отдельная тема — см. ревью DIR-05).

## Git workflow
`main`, коммиты: `feat(models): unique (external_chat_id, assigned_user_id) on dialogs + migration`, `fix(bridge): scope dialog upsert to manager + integrity retry`.

## Steps

### Step 1: Красные тесты на реальной БД

Новый `tests/unit/test_incoming_handler_db.py` — fixture как в `test_outbox_repo_sqlalchemy.py` (in-memory SQLite, create_all). Сценарии (всех — через `IncomingHandler` с реальной сессией: `IncomingHandler(session_mgr=MagicMock(), b24sync=AsyncMock(process_inbound→None), db_session_factory=lambda: session_ctx)` — но нужен session-per-call паттерн; проще: `db_session_factory` = фабрика, открывающая новую сессию того же engine (StaticPool даст ту же in-memory БД). Образец работы с сессией — `test_dialogs_api.py`):

1. `test_same_client_two_managers_two_dialogs`: seed Manager×2 (+TgAccount×2), одно `IncomingMessage(external_chat_id="111", sender_tg_id=...)`, обработать через handler для account(manager_id=1), затем то же для manager_id=2 → **два** Dialog-а с разными assigned_user_id (сейчас будет 1 — красный).
2. `test_concurrent_duplicate_insert_resolved`: вручную вставить 2 диалога с одинаковым (chat_id, manager) имитируя старую гонку, вызвать persist → не должно падать MultipleResultsFound, а переиспользовать один (док-требование к retry-логике; после миграции дублей не будет, но код обязан пережить legacy-данные).
3. `test_existing_dialog_of_other_manager_not_reused`: диалог chat_id="111" принадлежит manager 1; сообщение на manager 2 → создаётся новый диалог, диалог 1 не тронут (crm_deal_id manager 1 не перезаписан).

**Verify**: `pytest tests/unit/test_incoming_handler_db.py -q` → красные (минимум тест 1).

### Step 2: Модель + миграция

1. `dialog.py`: добавить `UniqueConstraint` — стиль проекта: отдельные импорты от `sqlalchemy`. В `__table_args__`:
```python
class Dialog(Base, TimestampMixin):
    __tablename__ = "dialogs"
    __table_args__ = (
        UniqueConstraint("external_chat_id", "assigned_user_id",
                         name="uq_dialogs_chat_per_manager"),
    )
```
Плюс индекс `Index("ix_dialogs_chat_manager", "external_chat_id", "assigned_user_id")` не нужен — unique сам создаёт индекс в postgres; для SQLite тоже. `external_chat_id` оставь как есть (не unique).
2. Миграция руками (`alembic/versions/`, revision = новый uuid-12, down_revision = текущий head — узнай: `.venv/Scripts/python.exe -m alembic heads`):
```python
def upgrade() -> None:
    # 1. Дедуп legacy: оставить минимальный id на пару (chat_id, manager)
    op.execute("""
        DELETE FROM dialogs d USING dialogs d2
        WHERE d.external_chat_id = d2.external_chat_id
          AND d.assigned_user_id IS NOT DISTINCT FROM d2.assigned_user_id
          AND d.id > d2.id
    """)
    # 2. Констрейнт
    op.create_unique_constraint("uq_dialogs_chat_per_manager", "dialogs",
                                ["external_chat_id", "assigned_user_id"])

def downgrade() -> None:
    op.drop_constraint("uq_dialogs_chat_per_manager", "dialogs")
```
Проверь образец стиля в `7f79d9761e13`. Note: postgres `DELETE ... USING` — ок; на SQLite этот SQL не выполняется (миграции гоняются только на postgres/VM — тесты используют create_all, не alembic). Отметь это комментарием в миграции.

**Verify**: `pytest -q` — модель-тесты зелёные (create_all подхватит констрейнт в SQLite).

### Step 3: Scoped upsert + IntegrityError retry в `_persist`

Заменить блок выбора диалога:
```python
existing_dialog = await session.execute(
    select(Dialog).where(
        Dialog.external_chat_id == msg.external_chat_id,
        Dialog.assigned_user_id == manager_id,
    )
)
dialog = existing_dialog.scalar_one_or_none()
if dialog is None:
    dialog = Dialog(..., assigned_user_id=manager_id)
    session.add(dialog)
    try:
        await session.flush()
    except IntegrityError:
        # гонка: параллельная задача уже вставила этот диалог — берём его
        await session.rollback()
        dialog = (await session.execute(
            select(Dialog).where(
                Dialog.external_chat_id == msg.external_chat_id,
                Dialog.assigned_user_id == manager_id,
            )
        )).scalar_one()
```
(rollback откатит и contact-flush из той же транзакции — поэтому после rollback нужно заново получить/создать Contact: если сложно аккуратно, альтернатива: ловить IntegrityError только на dialog-flush, а contact повторно select-ить. Сделай так, чтобы оба объекта оказались в сессии корректно; если треугольник rollback/contact мешает — переставь порядок: dialog вставляй ПЕРВЫМ, потом contact. Главное — тесты из Step 1 зелёные.) Импорт: `from sqlalchemy.exc import IntegrityError`.

**Verify**: `pytest tests/unit/test_incoming_handler_db.py -q` → все 3 зелёные.

### Step 4: Полный прогон + деплой-заметка

`pytest -q` (все, включая старые 86+), `ruff check`. В DEPLOY.md «Операции» добавь: после деплоя этого плана выполнить `docker compose exec web alembic upgrade head` (миграция задедупит legacy).

**Verify**: полный suite green; `git status` — только in-scope.

## Test plan
3 новых БД-теста (выше) + существующий `test_incoming_handler.py` остаётся зелёным (моки не видят изменений). Миграция проверяется оператором на VM (`alembic upgrade head` без ошибок, `\d dialogs` показывает констрейнт).

## Done criteria
- [ ] `pytest -q` green; `ruff check` exit 0
- [ ] `grep -n "uq_dialogs_chat_per_manager" src/app/models/dialog.py alembic/versions/*.py` — обе находки
- [ ] `grep -n "assigned_user_id == manager_id" src/app/bridge/incoming_handler.py` — скоуп в upsert
- [ ] Миграция применена на VM (оператор), в `plans/README.md` — DONE после этого

## STOP conditions
- IntegrityError-retry конфликтует с contact-upsert в одной транзакции так, что тест 3 не зелёнет после разумных попыток — доложи оба варианта перестановки.
- В БД прода уже есть дубиликаты с РАЗНЫМИ crm_deal_id на одной паре (дедуп удалит данные) — STOP, доложи, решит оператор.
- `alembic heads` показывает не `7f79d9761e13` (история миграций дрейфнула) — подставь актуальный head, но доложи.

## Maintenance notes
- Констрейнт делает `(chat_id, manager)` каноническим ключом диалога; любые новые писатели диалогов обязаны использовать его (см. DIR-05: при MAX появится `messenger` в ключе — констрейнт станет трёхколоночным, спланировать миграцию).
- Дедуп-миграция необратима (удалённые дубли не восстановить downgrade-ом) — отмечено в самой миграции.
