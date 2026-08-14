# Деплой Bitrix-TG (production)

Production: **https://b24-tg.haragy.top**
VM: `<VM_SSH_TARGET>`, Ubuntu 24.04, 2 vCPU / 2 GB RAM / 40 GB SSD.

## Что развёрнуто (Phase 4)
5 Docker-контейнеров через `docker compose`:
- `nginx` — TLS (Let's Encrypt) + reverse-proxy на `web:8000`, порты 80/443
- `web` — FastAPI (app.main web), порт 8000 только внутри сети
- `bridge` — Telethon/outbox-воркер (app.main bridge)
- `postgres:16-alpine` — БД, только внутри сети (без публичного порта)
- `redis:7-alpine` — pubsub/кэш, только внутри сети

Код: `/opt/bitrix-tg` (git clone GitHub). Конфиг: `/opt/bitrix-tg/.env` (chmod 600, НЕ в git).
Сертификат: `/etc/letsencrypt/live/b24-tg.haragy.top/`, автопродление через `certbot.timer`.

## Endpoints (production)
| Метод | Путь | Назначение |
|---|---|---|
| GET | `https://b24-tg.haragy.top/health` | Health-check (публичный) |
| POST | `/placement/deal` | B24 placement handler (CRM_DEAL_DETAIL_TAB) — вкладка в карточке сделки |
| GET | `/api/dialogs` | Список диалогов менеджера (нужна сессионная кука) |
| GET | `/api/dialogs/{id}/messages` | История + poll (`?since=`) |
| POST | `/api/dialogs/{id}/messages` | Отправить сообщение (→ outbox → Telegram) |
| GET | `/api/templates` | Шаблоны быстрых ответов |

`/dev/login` отключён в prod (`DEV_MODE=false` → 404).

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
4. `docker compose restart bridge`.

### 2. Смена B24-пароля (рекомендация безопасности)
Учётка `<admin-email>` использовалась для headless-OAuth. Сменить пароль в B24 после деплоя.

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
python3 scripts/gen_prod_env.py   # нужно пересоздать volume pg_data после

# резервная копия БД
docker compose exec postgres pg_dump -U bitrix_tg bitrix_tg > backup_$(date +%F).sql

# обновить сертификат вручную (если автопродление не сработало)
docker compose stop nginx
certbot renew
docker compose start nginx
```

## Архитектура деплоя (схема)
```
Internet → nginx:443 (TLS) → web:8000 (FastAPI)
                         ↘ /placement/deal ← Bitrix24 iFrame (CRM_DEAL_DETAIL_TAB)
Postgres ← web, bridge
Redis    ← web, bridge (готов под WebSocket в Phase 3b)
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
- ✅ Все 5 контейнеров Up, postgres healthy
- ✅ Сертификат валиден до 2026-11-09, автопродление активно
