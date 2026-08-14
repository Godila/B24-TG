# Plan 008: Гигиена — dead config/deps (Redis и пр.), hermetic-тесты (conftest), CI, docs-чистка

> **Executor instructions**: «чистим, не чиним» — никаких поведенческих изменений продукта. Каждый шаг Verify. STOP — стоп и доклад. Обнови строку в `plans/README.md`.
>
> **Drift check**: `git diff --stat 24a661e..HEAD -- pyproject.toml docker-compose.yml src/app/config.py .env.example .github/ tests/conftest.py docs/`. Расхождение = STOP (кроме случаев, когда 006 добавил поля конфига — это ожидаемо).

## Status
- **Priority**: P2 | **Effort**: M | **Risk**: LOW
- **Depends on**: 006 по порядку (см. Dependency notes в README: удалять `tenacity`/`redis` можно только после того, как 006 определился)
- **Category**: tech-debt / dx / tests / docs
- **Planned at**: commit `24a661e`, 2026-08-14

## Why this matters

Репо врёт о себе: Redis развёрнут контейнером, обязателен в конфиге и нарисован в архитектурных диаграммах — и не используется ни одной строкой; `sentry_dsn`, `b24_oauth_redirect`, `structlog`, `tenacity` — мёртвые; `gen_prod_env.py` пишет 4 переменные, которых Settings не читает. Тестовый сьют не герметичен (нет conftest, 5 файлов дублируют env-бойлерплейт, зелёность зависит от машины). Нет CI — гейты (pytest/ruff) живут только в памяти автора. Спека обещает несуществующий `/oauth/callback` и WebSocket. Всё это ложные сигналы каждому новому читателю (человеку или модели) и трение при онбординге.

## Current state

- `docker-compose.yml:18-24` — сервис redis + volume `redis_data`; web/bridge `depends_on: redis` (:33, :48-49). После планов 001-007 compose мог измениться — сверься.
- `pyproject.toml:14-17` — `redis>=5.0`, `structlog>=24.1`, `tenacity>=8.2` (grep по src/: импортов нет; ЕСЛИ 006 ввёл tenacity — оставить).
- `src/app/config.py:32,33,54,55` — `b24_oauth_redirect`, `b24_webhook_secret` (после 001 ИСПОЛЬЗУЕТСЯ — не трогать!), `redis_url` (required!), `sentry_dsn`. Сверить актуальное состояние: поля, использованные планами 001/006 (`b24_min_call_interval`, `crm_sync_*`), — не трогать.
- `scripts/gen_prod_env.py:33-37` — пишет `B24_OAUTH_REDIRECT/B24_CLIENT_ENDPOINT/B24_MEMBER_ID/B24_ACCESS_TOKEN/B24_REFRESH_TOKEN` (после 001 формат мог измениться — свериться; из них Settings читает только… проверить по config.py на момент исполнения).
- `.env.example` — после 001 там появились `CORS_ORIGINS/STATIC_DIR`; отсутствует `B24_WEBHOOK_SECRET` (появился в обиходе) — добавить.
- Тесты: `tests/conftest.py` НЕ существует. Бойлерплейт `monkeypatch.setenv(...)×8 + get_settings.cache_clear()` дублируется в `test_dialogs_api.py`, `test_auth.py`, `test_app_wiring.py`, `test_startup.py`, `test_placement.py`, `test_templates_api.py`.
- `.github/` не существует.
- `docs/superpowers/specs/2026-08-10-bitrix-tg-design.md:50` (`/oauth/callback`), `:303` (WebSocket §8.1 шаг 8); `README.md:39-45` (Redis в диаграмме), `:66-72` («статус доставки» — после 005 частично честно; после 006 — полностью); `docs/DEPLOY.md` (Redis-упоминания, раздел Обновление).
- Git log стиль: `fix(scope): ...`, `feat(scope): ...`, `docs: ...`.

## Commands

| Purpose | Command | Expected |
|---|---|---|
| Тесты | `.venv/Scripts/python.exe -m pytest -q` | all pass |
| Линт | `.venv/Scripts/ruff.exe check src/ tests/` | exit 0 |
| Проверка использования | `grep -rn "redis" src/ --include="*.py"` | пусто (после удаления поля) |

## Scope

**In scope**: `pyproject.toml`, `docker-compose.yml`, `src/app/config.py`, `scripts/gen_prod_env.py`, `.env.example`, `tests/conftest.py` (новый), 6 тестовых файлов (упрощение бойлерплейта), `.github/workflows/ci.yml` (новый), `README.md`, `docs/DEPLOY.md`, `docs/superpowers/specs/2026-08-10-bitrix-tg-design.md` (только аннотации), `src/app/main.py` (только если осталось упоминание redis в wiring — после фазы 5 его нет, проверить).

**Out of scope**: любой функционал; поведение тестов (ассерты не меняем — только env-сетап); планы 001-007 код (кроме перечисленных точек пересечения).

## Git workflow
`main`, коммиты: `chore: remove unused redis/structlog/sentry config and deps`, `test: hermetic conftest — single env setup`, `ci: github actions (ruff+pytest)`, `docs: align spec/README/DEPLOY with reality`.

## Steps

### Step 1: Мёртвые зависимости и конфиги

1. `pyproject.toml`: удалить `redis>=5.0`, `structlog>=24.1`; `tenacity` — удалить ТОЛЬКО если `grep -rn "tenacity" src/` пусто (006 мог ввести).
2. `config.py`: удалить `redis_url`, `sentry_dsn`, `b24_oauth_redirect` (grep-проверка каждого перед удалением: 0 использований вне config.py). **НЕ удалять** `b24_webhook_secret` (используется после 001) и новые поля 006.
3. `docker-compose.yml`: удалить сервис redis, volume `redis_data`, оба `depends_on: redis`. 
4. `gen_prod_env.py`: удалить строки переменных, которых нет в Settings (сверить по config.py на момент исполнения; REDIS_URL — удалить).
5. **Прод-влияние**: на VM `.env` содержит REDIS_URL — с `extra="ignore"` он молча игнорируется; redis-контейнер исчезнет при `up -d`. Зафиксировать в DEPLOY.md: «после pull: `docker compose up -d --build` удалит redis; REDIS_URL в .env можно убрать руками».

**Verify**: `grep -rn "redis" src/ pyproject.toml docker-compose.yml` → пусто; `pytest -q` green (тесты, сетящие REDIS_URL, перестанут это делать в Step 2; если падают ДО Step 2 — временно оставь setenv, удалишь в Step 2).

### Step 2: `tests/conftest.py` — герметичный сьют

Новый файл:
```python
"""Общий env-сетап для всех тестов: сьют не зависит от машины/локального .env.

Каждый тест, которому нужны ДРУГИЕ значения (DEV_MODE, CORS_ORIGINS),
переопределяет их сам через monkeypatch — autouse-фикстура ставит базу.
"""
import pytest

BASE_ENV = {
    "TG_API_ID": "1", "TG_API_HASH": "test",
    "TG_SESSIONS_DIR": "/tmp/tg_sessions",
    "B24_PORTAL": "https://test-portal.bitrix24.ru",
    "B24_CLIENT_ID": "test-client", "B24_CLIENT_SECRET": "test-secret",
    "DATABASE_URL": "sqlite+aiosqlite:///:memory:",
    "SESSION_SECRET": "test-session-secret",
    "DEV_MODE": "false",
}

@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch):
    for k, v in BASE_ENV.items():
        monkeypatch.setenv(k, v)
    from app.config import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
```
Затем в 6 файлах удалить дублирующиеся `monkeypatch.setenv` (кроме переопределяемых — например test_placement ставит DEV_MODE=true/CORS, test_app_wiring — CORS: оставить ТОЛЬКО отличия от BASE_ENV). `test_startup.py` использует `monkeypatch.setenv` — там тоже сократить. Проверь, что `test_telegram_provider`/юниты без env не задеты.

**Verify**: `pytest -q` green — столько же тестов, ни один не потерян; `grep -c "setenv" tests/integration/*.py` — суммарно ≤ 15 (было ~40).

### Step 3: CI

`.github/workflows/ci.yml`:
```yaml
name: CI
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.11" }
      - run: pip install -e ".[dev]"
      - run: ruff check src/ tests/
      - run: pytest -q
```
(на ubuntu pytest запускается как `pytest` — venv не нужен; SQLite-сьют самодостаточен).

**Verify**: файл синтаксичен (YAML — визуально + `python -c "import yaml,sys; yaml.safe_load(open('.github/workflows/ci.yml'))"`, если pyyaml есть, иначе пропусти); пуш оператора запустит (первый прогон — оператор проверит вкладку Actions).

### Step 4: Доки — привести к реальности

1. Спека `2026-08-10-bitrix-tg-design.md`: НЕ переписывать историю — добавить в конец раздел «## Пост-ревью поправки (2026-08)»: пункты «§2.1 /oauth/callback — не реализован, токены приходят через ONAPPINSTALL/headless», «§8.1 шаг 8 (WebSocket/Redis) — заменён polling; Redis удалён», «§8.2 шаги 5-6 — закрыты планами 005/006», «§9 MAX — реальная оценка L (см. ревью)».
2. `README.md`: убрать Redis из mermaid-диаграммы и упоминаний; строку про «86 тестов» → «90+»; раздел Развёртывание — сверить с DEPLOY.md (там уже актуально после 001).
3. `docs/DEPLOY.md`: убрать redis из перечня контейнеров/схемы (5→4 контейнеров); убедиться, что «Обновление» содержит `alembic upgrade head` и `restart nginx`; добавить runbook-строку «Сессия TG инвалидируется → `docker compose exec web python -m app.main auth --phone <номер>` + `UPDATE tg_accounts SET status='active'` + restart bridge» (если ещё нет).

**Verify**: `grep -in "redis" README.md docs/DEPLOY.md` → пусто; `pytest -q` green; `ruff check` exit 0; полный `git status` — только in-scope.

## Test plan
Новых тестов нет (кроме герметичности: сьют обязан пройти в окружении БЕЗ локального `.env` — проверь: `mv .env .env.bak && pytest -q && mv .env.bak .env` → green).

## Done criteria
- [ ] `grep -rn "redis" src/ pyproject.toml docker-compose.yml README.md docs/DEPLOY.md` → пусто
- [ ] `pytest -q` green в окружении без `.env`
- [ ] `.github/workflows/ci.yml` существует
- [ ] `tests/conftest.py` существует, дубли setenv сокращены
- [ ] Спека дополнена разделом поправок; README без Redis
- [ ] Оператор: CI зелёный на GitHub после пуша; на VM redis-контейнер удалён
- [ ] `plans/README.md`: 008 → DONE

## STOP conditions
- Удаление поля/депа ломает импорт где-то, кроме ожидаемого (grep нашёл использование, которое аудита нет) — верни поле, запиши находку в доклад.
- Тесты падают без `.env` по причине, не связанной с env (например, тайминги/order) — доложи, не глушин.
- CI-прогон на GitHub красный по инфраструктурной причине (нет прав на Actions) — зафиксируй и передай оператору.

## Maintenance notes
- CI — единственный автоматический гейт; любые новые тесты обязаны быть герметичными (conftest база + переопределения), иначе CI упадёт на чистом раннере — это желаемое поведение.
- Если вернётся WebSocket (3b) — Redis вернётся осознанно: dep + сервис + config + диаграммы одним коммитом.
- `docker-compose.override.yml.example` — проверь на актуальность (dev-режим, DEV_MODE=true); поправь минимально, если рассинхрон.
