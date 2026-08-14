# Plan 010: Direction-spike — админ-QR-онбординг менеджеров (вместо SSH+SQL ритуала)

> **Executor instructions**: это SPIKE/DESIGN-план: цель — проверить жизнеспособность QR-флоу Telethon и зафиксировать дизайн, НЕ построить продакшн-фичу целиком. Deliverable: прототип эндпоинта + работающий QR в браузере (локально) + дизайновой док с открытыми вопросами. STOP-условия — стоп и доклад. Обнови строку в `plans/README.md`.
>
> **Drift check**: `git diff --stat 24a661e..HEAD -- src/app/messaging/telegram/ src/app/web/ scripts/seed_manager.py`. Расхождение = STOP.

## Status
- **Priority**: P3 | **Effort**: M (spike) | **Risk**: MED (QR-флоу Telethon имеет нюансы с таймаутами)
- **Depends on**: 009 (health-основа; формально — 004 для мультиаккаунт-семантики)
- **Category**: direction (spike → решение строить/не строить)
- **Planned at**: commit `24a661e`, 2026-08-14

## Why this matters

Подключение каждого менеджера сегодня — SSH → SQL (`UPDATE tg_accounts ...`) → интерактивный CLI (`auth --phone` внутри контейнера, ввод SMS-кода/2FA) → рестарт bridge. Пользователь продукта уже спросил «и так нужно делать для каждого номера?». При целевых ≤10 менеджерах и планованных кадровых перестановках (увольнение → перепривязка) это доминирующий операционный кост. Спека (§3.3) обещала «QR + 2FA». Telethon нативно умеет `qr_login()` — менеджер сканирует QR своим Telegram, никакого SMS-кода и SSH. Spike отвечает на вопрос: ложится ли QR-флоу на нашу архитектуру (web-процесс запускает login, bridge потом регистрирует сессию) и сколько это стоит.

## Current state

- `src/app/messaging/telegram/auth.py` — CLI `login(phone)`: ищет `TgAccount` по phone, создаёт сессию в `<sessions_dir>/account_<id>/session` (важный контракт путей!), интерактивный `client.start(phone=...)`.
- `src/app/bridge/session_manager.py` — `register(account)` идемпотентен; `sessions_dir` общий с auth.
- **Критический факт для spike**: web и bridge — РАЗНЫЕ контейнеры/процессы, а volume `tg_sessions` смонтирован ТОЛЬКО в bridge (`docker-compose.yml`). QR-логин из web-процесса запишет .session в файловую систему web — bridge его увидит только при следующем register (файл общий? НЕТ — volume не смонтирован в web). Это главный архитектурный вопрос спайка (см. Open questions).
- `src/app/web/deps.py` — `get_current_manager` (Manager с `role`: manager/supervisor — для админ-гейта).
- `scripts/seed_manager.py` — создаёт менеджера+аккаунт (B24_USER_ID=1 хардкод).
- Telethon: `TelegramClient.qr_login()` возвращает `QRLogin`-объект: `.url` (строка для QR), `.recreate()` (новый QR по истечении), ожидание `asyncio.Future`; успешное сканирование авторизует сессию сразу (без кода). Таймаут QR ~ 2-3 мин на итерацию.
- Тесты-образцы: integration-роуты `tests/integration/test_placement.py`; мок Telethon — `tests/unit/test_telegram_provider.py`.

## Commands

| Purpose | Command | Expected |
|---|---|---|
| Тесты | `.venv/Scripts/python.exe -m pytest -q` | all pass |
| Линт | `.venv/Scripts/ruff.exe check src/ tests/` | exit 0 |
| Локальный smoke | `python -m app.main web` + браузер `http://localhost:8000/dev/qr?b24_user_id=1` | страница с QR (лениво рендерится) |

## Scope

**In scope (spike)**: новый `src/app/web/routes/admin_qr.py` (роут `GET /dev/qr` — только DEV_MODE), новая `src/app/static/qr.html` (+vendored `qrcode.min.js`), `docker-compose.yml` (монтирование tg_sessions в web — эксперимент), `docs/DESIGN-ADMIN-QR.md` (главный deliverable), тесты-моки.

**Out of scope**: продакшн-админка (создание менеджеров, CRUD шаблонов), supervisor-гейт (в спайке DEV_MODE-гейт достаточно), отрисовка QR на сервере (генерация на клиенте), перепривязка при увольнении, MAX.

## Git workflow
`main`, коммиты: `spike(admin): QR-login prototype endpoint + page`, `docs: DESIGN-ADMIN-QR — findings & open questions`.

## Steps

### Step 1: Исследование QR-флоу Telethon (докод)

Прочитай в `.venv/Lib/site-packages/telethon/client/auth.py` методы `qr_login`/`_qr_login_impl`: сигнатура, таймауты, `wait`, `recreate`, что возвращает, как связан с `is_user_authorized`. Зафиксируй 5-7 фактов в `docs/DESIGN-ADMIN-QR.md` (раздел «Факты Telethon»), с указанием файлов/строк исходника.

**Verify**: раздел существует, факты с file:line.

### Step 2: Прототип роута + страница

1. `docker-compose.yml`: добавить volume `tg_sessions:/data/tg_sessions` сервису `web` (эксперимент спайка; в доке зафиксировать следствие — web получает доступ к сессиям, риск-примечание).
2. `src/app/web/routes/admin_qr.py`:
```python
router = APIRouter(prefix="/dev/qr", tags=["admin-qr"])

@router.get("")
async def qr_page():  # отдаёт static/qr.html
    ...

@router.get("/start")
async def qr_start(b24_user_id: int, phone: str):
    """Запускает qr_login для (существующего или созданного) TgAccount.
    Возвращает {qr_url, account_id}. Логин-корутина живёт в фоне;
    статус: GET /dev/qr/status?account_id=... → waiting|authorized|expired."""
```
Реализация: найти/создать Manager+TgAccount по b24_user_id/phone (по логике seed_manager), `TelegramClient(str(<sessions_dir>/account_<id>/session), api_id, hash)`, `qr = await client.qr_login()`; фоновая task (`asyncio.create_task`) ждёт `await qr.wait()` → по успеху `account.status=active` + сообщить bridge (в спайке: перезапуск bridge вручную; задокументировать), по таймауту — `qr.recreate()` (до 3 итераций). Глобальный dict `account_id → состояние` (в памяти; перезапуск web теряет — ок для спайка). Хранить ссылку на QRLogin для `/status`.
3. `qr.html` + `static/vendor/qrcode.min.js` (vendor с CDN как alpine в 007): форма (b24_user_id, phone) → `/start` → рендер QR из `qr_url` (библиотека qrcode.js) → poll `/status` каждые 2с → «Готово: аккаунт активен. Перезапустите bridge (docker compose restart bridge)».
4. Гейт: роуты только при `settings.dev_mode` (образец: placement GET-роут).

**Verify**: локально `python -m app.main web` (с DEV_MODE=true, локальный sessions_dir) → страница открывается, `/start` возвращает qr_url (без реального сканирования — просто проверка флоу запуска; реальное сканирование — шаг оператора).

### Step 3: Тесты-моки

`tests/unit/test_admin_qr.py`: мок `TelegramClient` (qr_login → объект с url/wait/recreate): (a) start создаёт аккаунт+менеджера при отсутствии; (b) wait-успех в фоне проставляет status=active; (c) не-dev_mode → 404. Не гонять реальный Telethon.

**Verify**: `pytest -q` green; `ruff check` exit 0.

### Step 4: Живой эксперимент (оператор)

На VM (DEV_MODE временно): открыть страницу, отсканировать QR реальным Telegram → сессия создана, `tg_accounts.status=active`, `restart bridge` → `Registered session`. Результаты (успех/грабли/время) — в DESIGN-ADMIN-QR.md.

### Step 5: Дизайн-док (главный deliverable)

`docs/DESIGN-ADMIN-QR.md`: (1) факты Telethon (Step 1); (2) архитектурное решение о web-vs-bridge (где жить QR-логину; варианты: смонтировать volume в web (сделано в спайке) vs RPC-команда bridge через БД-таблицу команд vs отдельный login-контейнер; сравнение по рискам); (3) supervisor-гейт для прода; (4) открытые вопросы (список ниже); (5) вердикт: строить полноценную админку да/нет и оценка.

## Test plan
3 юнит-теста с моками (выше). Живое сканирование — ручной шаг оператора.

## Done criteria (spike)
- [ ] `docs/DESIGN-ADMIN-QR.md` существует с фактами/сравнением/вопросами/вердиктом
- [ ] Роут + страница работают локально (smoke-команда из Commands)
- [ ] `pytest -q` green; `ruff check` exit 0
- [ ] `plans/README.md`: 010 → DONE (со ссылкой на вердикт доки)

## STOP conditions
- `qr_login` в установленной версии Telethon отсутствует/сигнатура принципиально иная (см. Step 1) — зафиксируй и предложи SMS-код-флоу через браузер как альтернативу (та же архитектура, другой ввод).
- Volume-подход ломает изоляцию web-процесса неприемлемо (по мнению оператора) — не прорываться, оформить как открытый вопрос.

## Maintenance notes
- Решение «строить админку» принимается ПОСЛЕ спайка; полная версия включает: supervisor-гейт, создание менеджеров/шаблонов, live-статус в UI, отмену логина. Не начинать без вердикта.
- Если QR подтвердится — CLI `auth.py` остаётся fallback-ом (нужен для 2FA-пароля, который QR не покрывает: Telethon QR + облачный пароль — проверить в Step 1/4 и зафиксировать!).
