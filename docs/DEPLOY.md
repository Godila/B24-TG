# Деплой Bitrix-TG (production)

Production: **https://b24-tg.haragy.top**
VM: `<VM_SSH_TARGET>`, Ubuntu 24.04, 2 vCPU / 2 GB RAM / 40 GB SSD.

## Что развёрнуто (Phase 4)
4 Docker-контейнера через `docker compose`:
- `nginx` — TLS (Let's Encrypt) + reverse-proxy на `web:8000`, порты 80/443
- `web` — FastAPI (app.main web), порт 8000 только внутри сети
- `bridge` — Telethon/outbox-воркер (app.main bridge)
- `postgres:16-alpine` — БД, только внутри сети (без публичного порта)

Код: `/opt/bitrix-tg` (git clone GitHub). Конфиг: `/opt/bitrix-tg/.env` (chmod 600, НЕ в git).
Сертификат: `/etc/letsencrypt/live/b24-tg.haragy.top/`, автопродление через `certbot.timer`.

## Endpoints (production)
| Метод | Путь | Назначение |
|---|---|---|
| GET | `https://b24-tg.haragy.top/health` | Health-check (публичный) |
| POST | `/placement/deal` | B24 placement handler (CRM_DEAL_DETAIL_TAB) — вкладка в карточке сделки |
| POST | `/placement/app` | B24 placement handler (единственный LEFT_MENU) — оболочка с вкладками «Чаты»/«Панель» |
| POST | `/placement/chats` | «Чаты» (общий мессенджер) — прямая ссылка/iframe-вкладка оболочки |
| POST | `/webhook/b24/onappinstall` | Установка приложения. Требует заголовок `X-Webhook-Secret` (= `B24_WEBHOOK_SECRET` из .env): Bitrix24 сам этот заголовок не шлёт, поэтому при ручной переустановке приложения передавай его явно (curl) |
| GET | `/api/dialogs` | Список диалогов менеджера (нужна сессионная кука) |
| GET | `/api/dialogs/{id}/messages` | История + poll (`?since=`) |
| POST | `/api/dialogs/{id}/messages` | Отправить сообщение (→ outbox → Telegram) |
| POST | `/api/dialogs/{id}/media` | Отправить вложение (multipart `file` + `caption`; TG-диалоги, ≤25 МБ, MIME-allowlist) |
| GET | `/api/attachments/{id}/file` | Раздача вложения (только владелец диалога или supervisor; inline — безопасные MIME, остальное скачивается) |
| GET | `/api/inbox/dialogs` | Список «Чатов» с агрегатами (менеджер — свои; supervisor — все) |
| POST | `/api/inbox/dialogs/{id}/read` | Гасить непрочитанные (только ответственный) |
| GET | `/api/templates` | Шаблоны быстрых ответов |

`/dev/login` отключён в prod (`DEV_MODE=false` → 404).

### Иконка пункта левого меню Битрикс24 — НЕ НАСТРАИВАЕТСЯ (платформа)

Проверено 2026-08-17 трижды: (1) `placement.bind` не принимает иконку, а для
LEFT_MENU доки прямо запрещают и `OPTIONS` («значения не сохраняются»);
(2) в текущей форме изменения локального приложения (Разработчикам → вкладка
«Интеграции» → Изменить) полей «Название»/«Иконка» нет — только обработчик,
установка, client_id/secret и права; (3) иконки пунктов меню показывают лишь
тиражные приложения Маркета. Локальное приложение живёт со стандартной
иконкой Б24; бренд-знак встречает пользователя внутри — шапка оболочки
`/placement/app` + favicon (см. `src/app/static/brand/`).

Чтобы найти форму изменения локального приложения (если понадобится):
`https://<портал>/marketplace/dev/` → вкладка «Интеграции» сверху
(НЕ плитка «Другое» — та только создаёт новое; НЕ «Установленные» —
это маркетплейс).

CORS: с Plan 001 умолчание `CORS_ORIGINS` — пустое (CORS отключён, только same-origin).
На VM `.env` уже содержит `CORS_ORIGINS=https://b24-ye2jjz.bitrix24.ru,https://b24-tg.haragy.top`
(генерируется `scripts/gen_prod_env.py`) — поведение прода не меняется.

## Обновление кода
На VM:
```bash
cd /opt/bitrix-tg
git pull origin main
docker compose up -d --build          # пересобирает образ при изменениях
# миграции (если есть новые):
docker compose exec web alembic upgrade head
# рестарт nginx если web пересоздан (обновить DNS upstream):
docker compose restart nginx
```

> Plan 008: после pull `docker compose up -d --build` удалит redis-контейнер
> (больше не описан в compose). Приложение его не использовало, ничего не
> сломается; `REDIS_URL` в `.env` можно убрать руками (Settings игнорирует
> неизвестные переменные). Volume `redis_data` при желании удалить:
> `docker volume rm bitrix-tg_redis_data`.

### Runbook: сессия Telegram инвалидировалась (логаут/смена номера)
```bash
docker compose exec web python -m app.main auth --phone <номер>
docker compose exec postgres psql -U bitrix_tg -d bitrix_tg -c \
  "UPDATE tg_accounts SET status='active' WHERE phone='<номер>';"
docker compose restart bridge
```

## Что ещё нужно подключить (не сделано в Phase 4-5)

### 1. Реальный Telegram-аккаунт (отправка/приём сообщений)
Сейчас `TG_API_ID=12345`, `TG_API_HASH=placeholder`, а seeded `TgAccount` имеет `status=offline` → bridge видит 0 активных аккаунтов и не подключается к MTProto. Подключение:
1. Получить `api_id` + `api_hash` на https://my.telegram.org (один набор на команду).
2. Обновить `.env`: `TG_API_ID`, `TG_API_HASH`.
3. Обновить seeded-аккаунт на реальный номер и пройти первый вход (CLI auth):
   ```bash
   docker compose exec web python -m app.main auth
   ```
   (ввести номер менеджера → код из SMS/Telegram → 2FA если есть). `.session` сохранится в volume `tg_sessions`.
4. **Перевести аккаунт в `active`** (без этого bridge его не подхватит — `load_active_accounts` фильтрует по `status=active`):
   ```sql
   -- docker compose exec postgres psql -U bitrix_tg -d bitrix_tg
   UPDATE tg_accounts SET status='active', phone='<реальный_номер>' WHERE id=1;
   ```
5. `docker compose restart bridge` → в логах должно появиться `Registered session for account_id=1` и `Bridge started: 1 account(s) registered`.
6. Отправить тестовое сообщение в виджете карточки сделки → клиенту в Telegram.

### 2. Смена B24-пароля (рекомендация безопасности)
Учётка `<admin-email>` использовалась для headless-OAuth. Сменить пароль в B24 после деплоя.

### 3. Чеклист оператора после Plan 001 (утёкший OAuth-секрет)
Секрет закоммитили в публичный репо (удалён из файлов в Plan 001, но он остался в git-истории).
Лечится только ротацией — удаление из файла историю не переписывает:

- [ ] Ротация секрета B24: Настройки → Разработчикам → приложение «Bitrix-TG Integration» →
      перегенерировать client_secret (или пересоздать приложение). Обновить `.env` на VM
      и `import_b24_tokens` при необходимости.
- [ ] Сменить пароль B24-аккаунта (рекомендация из Фазы 2, не подтверждена).
- [ ] После деплоя: `docker compose up -d --build && docker compose restart nginx`,
      проверить `curl -i https://b24-tg.haragy.top/health` (появился HSTS) и
      `curl -X POST https://b24-tg.haragy.top/webhook/b24/onappinstall` → 401.

## Операции (полезные команды на VM)

SSH-доступ и креденшлс — в приватных ops-заметках, не в репо.

```bash
# статус всех контейнеров
docker compose ps

# логи (live)
docker compose logs -f web
docker compose logs -f bridge

# войти в БД
docker compose exec postgres psql -U bitrix_tg -d bitrix_tg

# перегенерировать .env (с новыми случайными секретами — ВНИМАНИЕ: меняет POSTGRES_PASSWORD)
# С Plan 001 скрипт берёт B24_CLIENT_ID/B24_CLIENT_SECRET из окружения:
#   B24_CLIENT_ID=... B24_CLIENT_SECRET=... python3 scripts/gen_prod_env.py
python3 scripts/gen_prod_env.py   # нужно пересоздать volume pg_data после

# резервная копия БД
docker compose exec postgres pg_dump -U bitrix_tg bitrix_tg > backup_$(date +%F).sql

# Автоматический ночной бэкап (установлен 2026-08-16)
# Что кладёт: pg_dump (gzip) + tar docker-volume tg_sessions + tar media-тома + .env → /opt/bitrix-tg/backups/,
# ротация старше 7 дней. Cron: /etc/cron.d/bitrix-tg-backup, 03:30 nightly, лог /var/log/bitrix-tg-backup.log.
# Выгрузка копии с VM (опционально): создать /etc/bitrix-tg-backup.env с
#   BACKUP_UPLOAD_DST="user@host:/path/backups"
# (ssh-ключ root'а должен иметь доступ туда; scp только свежих файлов).
# Ручной запуск: /opt/bitrix-tg/scripts/backup.sh
# Ротация docker-логов: json-file max-size=10m × 3 (x-logging в docker-compose.yml).

# обновить сертификат вручную (если автопродление не сработало)
docker compose stop nginx
certbot renew
docker compose start nginx

# применить миграции БД (после деплоя Plan 004 обязательно: миграция
# uq_dialogs_chat_per_manager дедуплицирует legacy-диалоги — перед запуском
# сделать backup_$(date +%F).sql, дедуп необратим)
docker compose exec web alembic upgrade head
```

## Архитектура деплоя (схема)
```
Internet → nginx:443 (TLS) → web:8000 (FastAPI)
                         ↘ /placement/deal ← Bitrix24 iFrame (CRM_DEAL_DETAIL_TAB)
Postgres ← web, bridge
bridge   → Telegram MTProto (Telethon) [нужен реальный api_id/hash + номер]
Bitrix24 REST ← web/bridge (OAuth token в БД, авто-refresh)
```

## Smoke-тест (выполнен после деплоя)
- ✅ `https://b24-tg.haragy.top/health` → 200 `{"status":"ok"}`
- ✅ HTTP → HTTPS редирект → 301
- ✅ `/dev/login` → 404 (prod mode)
- ✅ `/api/dialogs` без куки → 401 "Не авторизован"
- ✅ placement POST с фейковым токеном → 403 "Недействительный B24 токен"
- ✅ `/static/app.js` → 200
- ✅ `placement.bind` → True (вкладка в карточке сделки зарегистрирована)
- ✅ Все контейнеры Up, postgres healthy
- ✅ Сертификат валиден до 2026-11-09, автопродление активно

## Деплой канала MAX (миграция c3a7f1d92e40)

⚠️ Ревизия переименовывает колонки (tg_message_id → external_message_id,
tg_user_id → external_user_id) — старый код с новой схемой несовместим.
Порядок деплоя БЕЗ окна несовместимости:

```bash
# на VM, из каталога проекта
pg_dump ... > backup_$(date +%F)_pre_max.sql   # бэкап перед миграцией (конвенция)
docker compose stop web bridge
git pull origin main
docker compose build          # миграции вшиты в образ: build ОБЯЗАТЕЛЬНО до alembic
docker compose run --rm web alembic upgrade head
docker compose up -d
docker compose restart nginx  # обновить DNS upstream на пересозданный web
docker compose logs -f bridge   # ждём "Registered session" / "Bridge started"
```

После деплоя:
- подключение MAX-аккаунта — страница `/admin/max` (сессионная кука B24,
  QR + 2FA); bridge подхватит аккаунт сам за ~20с (AccountSyncWorker);
- контроль: `docker compose logs bridge | grep -i max` и `GET /health`
  (счётчики аккаунтов теперь по обоим каналам);
- `websockets>=12` добавлен в pyproject — образ пересоберётся с ним.

Чувствительные дрейфы: `MAX_APP_VERSION` в .env при симптоме
`qr_login.disabled` (устарела версия web-клиента; актуальную добывают из
бандлов web.max.ru — рецепт в памяти проекта project-max-channel).

## Админ-панель + TG QR-онбординг (миграция d8e2b6a91c74)

Миграция только-расширения (is_readonly + login_commands) — применяется тем
же одним окном, порядок как у MAX-ревизии выше. После деплоя:

- панель: `https://b24-tg.haragy.top/admin` (открытая вкладка из B24;
  карточки TG/MAX — менеджерам, раздел «Менеджеры» — администратору);
- TG подключение: карточка Telegram → QR (bridge исполняет по командам в
  login_commands; телефон подтягивается сам после скана);
- отвязка TG = log_out через bridge-команду; MAX = деактивация (токен
  стирается);
- `tg_sessions` volume больше НЕ монтируется в web (вариант B): CLI-фолбэк
  аутентификации запускать в bridge:
  `docker compose exec bridge python -m app.main auth --phone +7...`;
- права: «только чтение» в разделе «Менеджеры» — POST сообщений для
  read-only менеджера возвращает 403, виджет прячет поле ввода;
- безопасность: мутирующие /admin/api и /api роуты сверяют Origin
  (кука SameSite=none для iframe B24 иначе открывала CSRF).

## Общий мессенджер «Чаты» (миграция a9d3f17c5e42)

Миграция только-расширения (`dialogs.last_read_msg_id` + backfill курсором
= MAX(messages.id) — история до релиза считается прочитанной). Порядок
деплоя общий («Обновление кода»), но после него ОБЯЗАТЕЛЕН ручной шаг
приведения левого меню (без него меню не переключится на оболочку):

```bash
docker compose exec web python /app/scripts/bind_chats_placement.py
```

⚠️ B24 рендерит ОДИН пункт левого меню на приложение (проверено живьём
2026-08-17: второй биндинг существовал, но пункт не появился). Поэтому
пункт «ЧатМост» ведёт на оболочку `/placement/app` — вкладки «Чаты»
(основная) и «Панель»; legacy-хендлеры /placement/admin и
/placement/chats скрипт отвязывает (сами страницы остаются доступны
прямыми ссылками и используются оболочкой как iframe-вкладки). Скрипт
идемпотентен; после него — F5 страницы портала (меню кэшируется).

- вход: пункт «ЧатМост» левого меню B24 (группа «Приложения») → вкладки
  «Чаты»/«Панель» (выбор запоминается в #hash);
- «Чаты»: менеджер видит свои диалоги, supervisor — все (пишет только в
  свои, чужие — чтение с плашкой; чипы-фильтр по ответственному);
- неотвеченные (последнее сообщение входящее) — секцией сверху, с
  возрастом ожидания «ждёт N мин» и красным счётчиком; непрочитанные
  гаснут при открытии диалога (только владелец);
- из шапки чата — переход в карточку сделки B24 (`crm_deal_id` есть у
  диалога после crm_sync); при росте неотвеченных — «(N)» в заголовке
  вкладки + звук;
- dev-вход без B24: `GET /dev/login?b24_user_id=<id>&page=inbox` (только
  DEV_MODE=true).
