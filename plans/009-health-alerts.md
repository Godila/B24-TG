# Plan 009: Health & alerts — реальные статусы сессий в /health + алерты админу в B24-чат

> **Executor instructions**: шаг за шагом с Verify. STOP — стоп и доклад. Обнови строку в `plans/README.md`.
>
> **Drift check**: `git diff --stat 24a661e..HEAD -- src/app/bridge/health_checker.py src/app/web/routes/health.py src/app/models/tg_account.py tests/`. Расхождение = STOP.

## Status
- **Priority**: P3 | **Effort**: S-M | **Risk**: LOW (аддитивно)
- **Depends on**: 004 (статусы tg_accounts уже семантически корректны после scoping; формально независим, но тестируется вместе)
- **Category**: direction / ops
- **Planned at**: commit `24a661e`, 2026-08-14

## Why this matters

Спека (§7.5) обещала «статус всех сессий, БД» и «алерты админу на session drop/ban/FloodWait». Реальность: `/health` возвращает статическое `{"status":"ok"}` без единой проверки — bridge может быть мёртв (0 аккаунтов, сессия инвалидирована, бан), а uptime-мониторинг зелёный; `HealthChecker` только логирует. Личные MTProto-аккаунты — хрупкая часть системы №1 по спеке, и именно её отказ невидим. Самый дешёвый полный путь без нового инфра: HealthChecker пишет реальный статус в `tg_accounts.status` (колонка уже есть), `/health` читает таблицу + БД, алерты идут через уже подключённый `ImService.notify_manager` в B24-чат админа.

## Current state

- `src/app/web/routes/health.py` — весь файл: роут `GET /health` → `{"status":"ok"}` (без БД).
- `src/app/bridge/health_checker.py` — `HealthChecker(session_manager, interval_sec=300)`, `run()` — sleep→`_check_once()`-цикл; `_check_once` итерирует `sm._providers`, читает `provider._client.is_connected()` (метод!), при False — `logger.warning`. Статус в БД НЕ пишет.
- `src/app/models/tg_account.py` — `TgAccountStatus(active/banned/offline)`; колонки `status`, `last_floodwait_at`.
- `src/app/b24/im.py` — `ImService.notify_manager(auth_token, user_id, message) -> int`.
- `src/app/b24/token_manager.py` — `TokenManager.get_token()`.
- Bridge wiring: `src/app/main.py:run_bridge()` — после фазы 5 строит sm/worker/health; сюда добавить зависимость health→(db+im) иaleyты.
- Web-процесс отдельный: `/health` исполняется в web, читает БД напрямую (`app.db.async_session`).
- Тесты-образцы: `tests/unit/test_health_checker.py` (существует с фазы 1 — моки провайдеров), `tests/integration/test_health.py`, integration с БД — `test_dialogs_api.py` (dependency_overrides get_session).

## Commands

| Purpose | Command | Expected |
|---|---|---|
| Тесты | `.venv/Scripts/python.exe -m pytest -q` | all pass |
| Линт | `.venv/Scripts/ruff.exe check src/ tests/` | exit 0 |

## Scope

**In scope**: `src/app/bridge/health_checker.py`, `src/app/web/routes/health.py`, `src/app/config.py` (+`alert_admin_b24_user_id: int = Field(1)`), `src/app/main.py` (wiring), `tests/unit/test_health_checker.py`, `tests/integration/test_health.py`, новый `tests/integration/test_health_db.py`.

**Out of scope**: `/metrics`-эндпоинт, Sentry, email/SMS-алерты, авто-reconnect сессий (HealthChecker по-прежнему не чинит — только детектит), UI-страница статусов.

## Git workflow
`main`, коммиты: `feat(health): persist session status to tg_accounts + B24 alerts`, `feat(web): /health reports db + account statuses (degraded)`.

## Steps

### Step 1: HealthChecker пишет статусы + алертит

1. Конструктор: `HealthChecker(session_manager, interval_sec=300, *, session_factory=None, notifier=None, admin_user_id=None)` — все три новые зависимости опциональны (None → старое поведение, только логи; обратная совместимость тестов).
2. `_check_once` для каждого `(account_id, provider)`:
   - `connected = provider._client.is_connected()` (существующая логика, getattr-защита остаётся);
   - если `session_factory` передана: SELECT TgAccount by id; `new_status = active если connected иначе offline`; при переходе `active→offline` (старое в БД было active) — UPDATE status + `logger.error("Account %s went offline", account_id)`;
   - если переход случился И `notifier` передан: `await notifier(admin_user_id, f"⚠️ Bitrix-TG: TG-аккаунт id={account_id} ({account.phone}) отключён — проверьте сессию")` — notifier-колбэк вида `async def admin_alert(user_id: int, text: str)` (его реализация в main.py: TokenManager→ImService.notify_manager; ошибки нотификации глотать с логом, не ронять чекер).
3. Обратный переход offline→active — просто UPDATE (без алерта).
4. Тесты `tests/unit/test_health_checker.py`: (a) connected → status остаётся/становится active, алерта нет; (b) disconnect при active в БД → status=offline + notifier вызван с упоминанием account_id; (c) notifier=None → не падает; (d) повторный чек при уже offline — алерт НЕ повторяется (edge: только на переход).

**Verify**: `pytest tests/unit/test_health_checker.py -q` → pass.

### Step 2: `/health` — реальный

1. `health.py`: роут становится async с `Depends(get_session)`:
```python
@router.get("/health")
async def health(session: AsyncSession = Depends(get_session)) -> dict:
    try:
        await session.execute(select(1))
    except Exception:
        return JSONResponse({"status": "error", "db": "down"}, status_code=503)
    accounts = (await session.execute(select(TgAccount))).scalars().all()
    active = [a for a in accounts if a.status == TgAccountStatus.active]
    offline_active = [a.id for a in active if ???]  # см. пункт 2
```
   Сема: web-процесс НЕ знает is_connected (это bridge). Договорённость: bridge пишет `tg_accounts.status`; degraded = «в таблице есть аккаунты со status=active, но их НЕТ в живых» — web не видит живость. Поэтому契约 проще: `/health` сообщает `{"status": "ok"|"degraded", "db": "ok", "accounts": {"total": N, "active": N, "offline": N, "banned": N}}`; `degraded` если `active==0 and total>0` (нет ни одной рабочей сессии) ИЛИ есть `banned>0`. Плюс отдельное поле `bridge_heartbeat`: НЕ вводим (bridge пишет status — этого достаточно для v1).
2. Интеграционный тест `tests/integration/test_health_db.py`: dependency_overrides get_session на in-memory с seed: (a) пусто → ok; (b) 1 active → ok; (c) 1 offline 0 active → 503 degraded; (d) banned → degraded.
3. Существующий `tests/integration/test_health.py` обновить (теперь нужен override сессии или пустая БД → ok).

**Verify**: `pytest tests/integration/test_health.py tests/integration/test_health_db.py -q` → pass.

### Step 3: Wiring в run_bridge

`main.py`: построить `admin_alert` (TokenManager + ImService, тот же клиент, что у crm — см. 006), `HealthChecker(sm, interval_sec=300, session_factory=async_session, notifier=admin_alert, admin_user_id=settings.alert_admin_b24_user_id)`; config +`alert_admin_b24_user_id: int = Field(1)`; `.env.example` + комментарий. `test_startup.py` — моки на новые аргументы.

**Verify**: `pytest -q` — весь suite green; `ruff check` exit 0.

## Test plan
4 юнит-теста HealthChecker (переходы/алерты/None-совместимость), 4 интеграционных /health (db-down не эмулируем — только статусы; db-ok через override), обновлённый startup. Образцы указаны в Current state.

## Done criteria
- [ ] `pytest -q` green; `ruff check` exit 0
- [ ] `curl -s https://b24-tg.haragy.top/health` (оператор, после деплоя) → JSON с accounts-счётчиками
- [ ] При переводе аккаунта в offline (в БД руками + рестарт bridge) — админ получает сообщение в B24-чат (оператор проверяет)
- [ ] `plans/README.md`: 009 → DONE

## STOP conditions
- Telethon `is_connected()` ведёт себя не как метод (расхождение с ревью-проверкой) — STOP, доложи сигнатуру.
- Ассерты старого `test_health.py` невозможно обновить малой кровью (форма ответа кому-то нужна?) — grep потребителей `/health` (uptime-сервисы) перед сменой формы; если внешний монитор парсит `{"status":"ok"}` — сохрани поле `status` с теми же значениями ok/error (degraded добавить, ok оставить).

## Maintenance notes
- `/health` теперь ходит в БД — держи его дешёвым (один select); если добавится больше проверок — выносить в фоновый кэш статуса.
- Алерты идут в B24-чат админа — если портал ляжет, алерты тоже (осознанный trade-off v1; внешний канал — при росте).
- При подключении второго+ менеджера: алерты приходят админу (b24_user_id из конфига), не владельцу аккаунта — при желании расширить `notifier(account.manager.b24_user_id, ...)` отдельным шагом.
