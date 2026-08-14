# Implementation Plans — Bitrix-TG post-audit

Сгенерированы skill'ом `improve` 2026-08-14 по итогам полного архитектурного ревью (коммит `24a661e`). Проект: замена Wazzup (TG ↔ Bitrix24), фазы 1-5 завершены, production на https://b24-tg.haragy.top.

Выполнять в порядке ниже (зависимости соблюдены). Executor: прочитай план целиком перед стартом, соблюдай STOP-условия, обновляй свой статус здесь. Планы самодостаточны — сессия-автор не нужна.

## Execution order & status

| Plan | Title | Priority | Effort | Depends on | Status |
|------|-------|----------|--------|------------|--------|
| 001 | Security: ротация утёкшего секрета + харденинг (webhook auth, cookie secure, CORS, HSTS) | P1 | S+M | — | DONE (код, a6a9724; остаётся оператору: ротация секрета + деплой) |
| 002 | Починить входящий конвейер: Telethon NewMessage(incoming=True) + тесты шва | P1 | S | — | DONE (24c8e0e) |
| 003 | Spike: верификация B24 free-tier CRM-методов на прод-портале | P1 | S | — | DONE (фаза B: все 8 методов OK на free-tier; поймал и закрыл баг JSON-body клиента, см. docs/B24-FREE-TIER.md) |
| 004 | Мультиаккаунт: dialog scoping по менеджеру + unique constraint | P1 | M | 002 | DONE (11ef4bc, b2cb637; миграцию применить на VM при деплое) |
| 005 | Замкнуть исходящий TG-цикл: Message.status/tg_message_id/message_id + attempts-фикс | P1 | M | — | DONE (09db7cb, 13f3993, 68a641a) |
| 006 | CRM durability: очередь crm_sync + retry, rate-limit B24, shared httpx, outbound timeline | P2 | M-L | 005 | DONE (27ae5c5..192445e; миграцию crm_sync применить на VM при деплое) |
| 007 | UX-патчи: новейшая история, vendored Alpine, media-placeholder | P2 | S-M | — | DONE (1d44d76, d45e3a3, c3bf135) |
| 008 | Гигиена: dead config/deps (Redis и пр.), tests/conftest.py, CI, docs-чистка | P2 | M | 006 (порядок) | DONE (a6bfdf9..988aaef; CI заработает после пуша оператором) |
| 009 | Health & alerts: реальные статусы сессий в /health + алерты админу | P3 | S-M | 004 | TODO |
| 010 | Direction-spike: админ-QR-онбординг менеджеров (вместо SSH+SQL) | P3 | M (spike) | 009 | TODO |

## Dependency notes

- **004 зависит от 002**: тесты inbound-пайплайна из 002 дают сетку для сценариев мультиаккаунта из 004.
- **006 зависит от 005**: crm_sync(outbound) использует `OutboxItem.message_id`, введённый в 005.
- **008 после 006**: 006 может задействовать `tenacity` (сейчас dead-dep); удалять его можно только если 006 решит обойтись без него. Поле `redis_url` удаляется в 008 — к этому моменту 006 уже определился с зависимостями.
- **003 (spike) стоит запустить параллельно с 001/002**: результат влияет на 006 (если free-tier режет методы — durability станет критичнее/деградация).
- Рекомендуемая последовательность для «сделать продукт живым»: 001 → 002 → 003 → 004 → 005 → подключение реального TG-номера (оператор, см. docs/DEPLOY.md) → e2e-проверка → 006+.

## Контекст для всех планов (общие факты)

- Windows-машина разработки, venv: `.venv/Scripts/python.exe`, ruff: `.venv/Scripts/ruff.exe`. Команды — из корня репо `C:\Users\geor\Desktop\Bitrix-TG`.
- Тесты: `.venv/Scripts/python.exe -m pytest -q` (сейчас 86 passed). Линт: `.venv/Scripts/ruff.exe check src/ tests/`.
- Git: работа прямо в `main` (однопользовательский flow), conventional commits вида `fix(scope): ...` (см. `git log`). НЕ пушить без указания оператора.
- Production: VM `<VM_SSH_TARGET>` (SSH root, ключ установлен), `/opt/bitrix-tg`, `docker compose up -d --build` + `docker compose restart nginx` после пересоздания web. Подробно: `docs/DEPLOY.md`.
- Стиль кода: русские docstring-ы, `logging.getLogger(__name__)`, SQLAlchemy 2.0 async (`Mapped`/`mapped_column`), тесты через in-memory SQLite + `dependency_overrides` (образец: `tests/integration/test_dialogs_api.py`).

## Findings considered and rejected (не перепроверять в следующем аудите)

- WebSocket/Redis-pubsub real-time: polling 3с достаточен для ≤10 менеджеров; вернуться при жалобах на латентность.
- Sentry: заменён дешёвле — алерты через ImService (план 009); dead `sentry_dsn` удаляется в 008.
- Non-root Docker (SEC-06): отложено осознанно — single-tenant VM, порты БД закрыты, выигрыш только defense-in-depth; вернуться при росте поверхности.
- Массовые рассылки: отложено самой спекой (§11, «этап 2»).
- WorkerOutboxRepository session-per-call: дёшево при ≤10-менеджерном объёме, pool 5+10 справляется.
- DTO-дедупликация (`dialogs.py` мапперы vs `schemas.py`): два маленьких маппера, консолидация — церемония.
- Poll-loop backoff в UI (`app.js`): стоимость тривиальна; только если сессии начнут истекать на практике.
- `/static/placement.html` без авторизации (косметика): рендерит мёртвый виджет, не data-leak.
- Alembic-дрейф: проверен, отсутствует (b24_tokens миграция соответствует модели).

## Ключевой вывод ревью (мотивация всего пакета)

Все критичные баги живут на швах между модулями, каждый из которых TDD-покрыт против моков: (1) inbound мёртв (Telethon Raw handler), (2) мультиаккаунт ломается на upsert диалогов, (3) исходящий цикл не замкнут (⏳ навсегда), (4) CRM-записи без retry при жёстких лимитах free-tier, (5) OAuth-секрет в публичном репо. Планы 001–005 закрывают это до состояния «можно подключать номер и верить e2e».
