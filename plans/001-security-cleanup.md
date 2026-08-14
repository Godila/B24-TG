# Plan 001: Security — убрать утёкший OAuth-секрет из репо + захардить auth-поверхности

> **Executor instructions**: выполняй шаги по порядку, прогоняй каждую Verify-команду и сверяй ожидаемый результат перед следующим шагом. При срабатывании STOP-условия — остановись и доложи, не импровизируй. По завершении обнови свою строку в `plans/README.md`.
>
> **Drift check (первым делом)**: `git diff --stat 24a661e..HEAD -- scripts/gen_prod_env.py src/app/web/routes/webhook.py src/app/web/session.py src/app/web/routes/placement.py src/app/web/app.py src/app/config.py nginx/nginx.conf docs/DEPLOY.md .env.example tests/`. Если эти файлы менялись — сверь «Current state» с живым кодом; расхождение = STOP.

## Status
- **Priority**: P1 | **Effort**: S+M | **Risk**: MED (webhook-валидация может отвергнуть легитимный B24-вызов — поэтому Pydantic-схема консервативна)
- **Depends on**: none
- **Category**: security
- **Planned at**: commit `24a661e`, 2026-08-14

## Why this matters

В публичном GitHub-репо (https://github.com/Godila/B24-TG) закоммичен реальный OAuth-секрет Bitrix24-приложения (`scripts/gen_prod_env.py:32`) плюс root-IP сервера и email админа (`docs/DEPLOY.md:4,62`). Секрет скомпрометирован — обязательна ротация (внешний шаг оператора). Параллельно три fail-open дыры: `/webhook/b24/onappinstall` принимает любой POST и перезаписывает OAuth-токены (кирпич интеграции одним запросом), сессионная кука без `secure`, CORS по умолчанию `*` с credentials. Всё закрывается малой кровью.

**Важно: секрет уже в git-истории — удаление из файла НЕ лечит. Лечит только ротация секрета в B24.** Удаление из файла прекращает дальнейшую утечку и чистит образ.

## Current state

- `scripts/gen_prod_env.py:31-32` — литералы `B24_CLIENT_ID=<утёкший префикс local.>` и `B24_CLIENT_SECRET=<утёкший литерал>` (значения НЕ копировать никуда). Строки 33-37: `B24_OAUTH_REDIRECT`, `B24_CLIENT_ENDPOINT`, `B24_MEMBER_ID`, `B24_ACCESS_TOKEN`, `B24_REFRESH_TOKEN` — тоже специфичны для прод-портала; `B24_MEMBER_ID/ACCESS_TOKEN/REFRESH_TOKEN/CLIENT_ENDPOINT` при этом вообще не читаются Settings (мёртвые — см. план 008), а `B24_PORTAL` в строке 29 нужен.
- `docs/DEPLOY.md:4` — «VM: `root@<VM_IP>`…»; `:62` — email админа; `:80-е` — примеры команд с этим IP (`ssh root@<VM_IP>`).
- `src/app/web/routes/webhook.py` (весь файл ~40 строк): роут `POST /webhook/b24/onappinstall`, читает `await request.json()`, берёт `payload["auth"]`, вызывает `tm.save_install_data(auth_data)` без какой-либо проверки. `payload.get("auth", {})` → `KeyError` в `save_install_data` при неполных данных → 500.
- `src/app/b24/token_manager.py:91-109` — `save_install_data` делает upsert по `member_id` (перезапись access/refresh).
- `src/app/config.py:33` — `b24_webhook_secret: str = Field("")` — конфиг существует, нигде не проверяется (grep: только определение).
- `src/app/web/session.py:72-85` — `create_session_cookie_params` возвращает dict с `httponly/samesite/max_age/path`, без `secure`. Вызывается из `placement.py:_set_session_and_respond` и `web/app.py:dev_login`.
- `src/app/web/routes/placement.py` — POST-роут (валидация B24-токена уже есть — не трогать), GET-роут dev (гейтится `dev_mode`).
- `src/app/web/app.py:29-36` — `CORSMiddleware(allow_origins=_parse_origins(settings.cors_origins), allow_credentials=True, ...)`.
- `src/app/config.py:48` — `cors_origins: str = Field("*")`.
- `nginx/nginx.conf:47-73` — блок 443: есть X-Frame-Options/CSP, нет `Strict-Transport-Security` и `X-Content-Type-Options`.
- `.env.example` — нет строки `CORS_ORIGINS` (и `STATIC_DIR`).
- Конвенции: русские docstring-ы; тесты integration через `TestClient` + `monkeypatch.setenv` + `get_settings.cache_clear()` (образец: `tests/integration/test_webhook.py`, `test_app_wiring.py`).

## Commands

| Purpose | Command | Expected |
|---|---|---|
| Тесты | `.venv/Scripts/python.exe -m pytest -q` | all pass (86+N new) |
| Линт | `.venv/Scripts/ruff.exe check src/ tests/` | exit 0 |
| Поиск секрета | `git grep -nE "<фингерпринты client_id|client_secret из контроллерской задачи>"` | нет вывода (после шага 1) |

## Scope

**In scope**: `scripts/gen_prod_env.py`, `docs/DEPLOY.md`, `src/app/web/routes/webhook.py`, `src/app/web/schemas.py` (новая схема), `src/app/web/session.py`, `src/app/web/routes/placement.py`, `src/app/web/app.py`, `src/app/config.py`, `nginx/nginx.conf`, `.env.example`, `tests/integration/test_webhook.py`, `tests/integration/test_app_wiring.py` (доп.), новый `tests/unit/test_webhook_validation.py` (или integration).

**Out of scope**: `token_manager.py` (логика save_install_data не меняется), placement-валидация токена (уже сделана), удаление мёртвых env-переменных из gen_prod_env кроме перечисленных (мёртвые чистятся в 008), git-история (переписывание истории НЕ делаем — лечится ротацией), `Dockerfile` (scripts нужны в образе; после ротации старый секрет невалиден).

## Git workflow
Прямо в `main`. Коммиты: `security: remove leaked B24 credentials from repo`, `security: authenticate onappinstall webhook`, `security: cookie secure + CORS fail-closed + HSTS`. Не пушить без оператора.

## Steps

### Step 1: Убрать литералы из gen_prod_env.py + DEPLOY.md

1. `scripts/gen_prod_env.py`: заменить блок литералов на чтение из окружения с подсказкой:
```python
CLIENT_ID = os.environ.get("B24_CLIENT_ID")
CLIENT_SECRET = os.environ.get("B24_CLIENT_SECRET")
if not CLIENT_ID or not CLIENT_SECRET:
    raise SystemExit("Задайте B24_CLIENT_ID и B24_CLIENT_SECRET в окружении (не храните в репо)")
```
и подставить `{CLIENT_ID}`/`{CLIENT_SECRET}` в шаблон env. `B24_PORTAL` тоже вынести в переменную `PORTAL = os.environ.get("B24_PORTAL", "https://b24-ye2jjz.bitrix24.ru")` (адрес портала не секрет, но пусть будет управляемым). Импорт `os` добавить.
2. `docs/DEPLOY.md`: заменить `root@<VM_IP>` → `<VM_SSH_TARGET>` (все вхождения, включая примеры команд), email на `<admin-email>`. Добавить в раздел «Операции» строку: «SSH-доступ и креденшлс — в приватных ops-заметках, не в репо».
3. Закоммитить.

**Verify**: `git grep -nE "<фингерпринты client_id|client_secret|IP из контроллерской задачи>"` → пусто; `.venv/Scripts/ruff.exe check scripts/` → exit 0 (если scripts не в ruff scope — пропусти ruff для scripts).

### Step 2: Аутентификация webhook `/webhook/b24/onappinstall`

1. В `src/app/web/schemas.py` добавить Pydantic-схему:
```python
class OnAppInstallAuth(BaseModel):
    """Поле auth из ONAPPINSTALL payload (строгая форма — лишнее/битое отвергаем)."""
    model_config = pydantic.ConfigDict(extra="forbid")

    access_token: str
    refresh_token: str
    member_id: str
    client_endpoint: str
    domain: str
    user_id: int
    expires_in: int
    scope: str
```
(сверь реальные ключи с `token_manager.py:91-109` — там `auth_data.get("expires_in")` с `int()`-кастом → в схеме тоже приводится; `user_id` может приходить строкой — используй `user_id: int` с `coerce_numbers_from_str`? Проще: `user_id: int` и в webhook перед схемой ничего не делать — pydantic v2 coercity по умолчанию строку "1" в int НЕ делает в strict, но в lax (default) — делает. Оставь default-lax.)
2. `webhook.py`: проверка секрета ДО парсинга:
```python
settings = get_settings()
secret = request.headers.get("X-Webhook-Secret", "")
if not settings.b24_webhook_secret or not hmac.compare_digest(secret, settings.b24_webhook_secret):
    return JSONResponse({"error": "unauthorized"}, status_code=401)
```
Затем `payload = await request.json()`; `auth = OnAppInstallAuth.model_validate(payload.get("auth", {}))` (ошибки валидации → 422 через `except ValidationError`); `await tm.save_install_data(auth.model_dump())`.
3. **ВНИМАНИЕ**: B24 не шлёт `X-Webhook-Secret` сам по себе — это наш собственный заголовок. Реальные install-вызовы B24 приходят без него → будут 401. Это осознанный trade-off: endpoint используется вручную (import_b24_tokens.py) при операциях переустановки. Отрази это комментарием в коде + строкой в DEPLOY.md: «при ручной переустановке приложения передай заголовок X-Webhook-Secret из .env». Альтернативу (проверка подписи B24) НЕ реализовывать — у B24 нет подписи ONAPPINSTALL.
4. Тесты `tests/integration/test_webhook.py`: переписать — (a) без заголовка → 401; (b) с заголовком и полным auth → 200 + `save_install_data` вызван (мок, как сейчас); (c) с заголовком и битым auth (нет refresh_token) → 422, `save_install_data` НЕ вызван.

**Verify**: `.venv/Scripts/python.exe -m pytest tests/integration/test_webhook.py -q` → pass (3 теста).

### Step 3: Cookie `secure` + HSTS + nosniff

1. `session.py`: `create_session_cookie_params(b24_user_id, deal_id, secret, *, secure: bool = True)` — добавить `"secure": secure` в dict.
2. `placement.py:_set_session_and_respond` и `web/app.py:dev_login`: передавать `secure=not settings.dev_mode` (dev-сервер на http://localhost).
3. `nginx/nginx.conf`, блок 443: добавить
```nginx
add_header Strict-Transport-Security "max-age=31536000" always;
add_header X-Content-Type-Options "nosniff" always;
```
4. Обнови тест `tests/integration/test_placement.py::test_placement_deal_prod_accepts_valid_token` — добавь ассерт `"Secure" in cookie_header` (в DEV_MODE-тестах Secure быть НЕ должно).

**Verify**: `pytest tests/integration/test_placement.py tests/integration/test_auth.py -q` → pass.

### Step 4: CORS fail-closed

1. `config.py:48`: `cors_origins: str = Field("", description="CORS origins через запятую; пусто = CORS отключён (только same-origin)")`.
2. `web/app.py`: если `origins` пуст → НЕ добавлять CORSMiddleware вовсе; иначе как сейчас (`allow_credentials=True`).
3. `.env.example`: добавить строки `CORS_ORIGINS=` (пустая с комментарием «для прод: https://<портал>.bitrix24.ru,https://<домен>») и `STATIC_DIR=src/app/static`.
4. Тесты `tests/integration/test_app_wiring.py`: существующие CORS-тесты ставят `CORS_ORIGINS=...` — они продолжат работать; добавь тест: без CORS_ORIGINS заголовок `access-control-allow-origin` отсутствует в ответе.
5. **Прод-влияние**: на VM `.env` уже содержит `CORS_ORIGINS=...` (сгенерирован) — поведение прода не изменится. Отрази в DEPLOY.md.

**Verify**: `.venv/Scripts/python.exe -m pytest tests/integration/test_app_wiring.py -q` → pass.

### Step 5: Финал + деплой-заметка

1. Полный прогон: `pytest -q`, `ruff check src/ tests/`.
2. В DEPLOY.md в раздел «Что ещё» добавь чеклист оператора (см. ниже «Оператор обязан сделать»).

**Verify**: полный suite green; `git status` — только in-scope файлы.

## Оператор обязан сделать (вне executor)

- [ ] Ротация секрета B24: Настройки → Разработчикам → приложение «Bitrix-TG Integration» → перегенерировать client_secret (или пересоздать приложение). Обновить `.env` на VM и `import_b24_tokens` при необходимости.
- [ ] Сменить пароль B24-аккаунта (рекомендация из Фазы 2, не подтверждена).
- [ ] После деплоя: `docker compose up -d --build && docker compose restart nginx`, проверить `curl -i https://b24-tg.haragy.top/health` (появился HSTS) и `curl -X POST .../webhook/b24/onappinstall` → 401.

## Test plan
Новые: 3 webhook-теста (401/200/422), 1 placement (Secure-флаг), 1 CORS-disabled, плюс негативные Secure-отсутствие в dev. Образец структур — `tests/integration/test_webhook.py` (текущий) и `test_app_wiring.py`.

## Done criteria
- [ ] `git grep -nE "<фингерпринты client_id|client_secret|IP из контроллерской задачи>"` — пусто
- [ ] `pytest -q` green; `ruff check src/ tests/` exit 0
- [ ] POST /webhook/b24/onappinstall без заголовка → 401 (тест)
- [ ] Кука в prod-режиме содержит Secure (тест)
- [ ] Без CORS_ORIGINS middleware не подключается (тест)
- [ ] DEPLOY.md без IP/email; плейсхолдеры на месте
- [ ] `plans/README.md` строка 001 → DONE (после деплоя и ротации — оператор)

## STOP conditions
- В `webhook.py`/`session.py`/`app.py` код не соответствует excerpt-ам выше.
- Pydantic-схема отвергает валидный (по token_manager) payload — разбери реальные ключи из `token_manager.py:91-109` прежде чем менять форму; если конфликт не решается консервативно — STOP.
- Тест Secure-флага падает из-за того, что dev-режим где-то читается иначе (не через `settings.dev_mode`) — STOP, доложи.

## Maintenance notes
- Заголовок `X-Webhook-Secret` — наше соглашение; задокументирован в DEPLOY.md. Если B24 когда-нибудь пришлёт подписанные вебхуки — замени проверку.
- `secure=not dev_mode` связывает два механизма; при появлении отдельного `COOKIE_SECURE` конфига — перенеси логику туда.
- Ротация секрета делает утёкший литерал бесполезным, но он остаётся в истории git навсегда — не «восстанавливай» его из истории.
