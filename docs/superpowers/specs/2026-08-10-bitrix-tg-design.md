# Design Spec: Замена Wazzup — Telegram ↔ Bitrix24 CRM

**Дата:** 2026-08-10
**Статус:** Утверждён (brainstorming complete)
**Рабочая папка:** `C:\Users\geor\Desktop\Bitrix-TG`

## 1. Постановка задачи

Заменить сервис Wazzup (chat-агрегатор) для Telegram-канала и интегрировать в собственную CRM Bitrix24 (облако). Wazzup — это SaaS-прослойка, которая подключает мессенджеры к CRM и собирает переписку с клиентами в едином окне, связывая её с карточками клиентов.

### Ключевое отличие от Wazzup
Wazzup **не хранит тексты переписки в Bitrix24** — в карточку попадает только пустой маркер «Wazzup message». Наша замена **записывает реальные тексты сообщений в timeline карточки сделки** (`crm.timeline.comment.add`). Это даёт менеджерам контекст прямо в CRM и открывает возможности аналитики/ИИ — преимущество над исходным сервисом.

### Подтверждённый объём
| Параметр | Значение |
|---|---|
| Мессенджер на старте | Telegram (личный аккаунт, MTProto) |
| Мессенджер на перспективу | MAX (российский) |
| CRM | Bitrix24 **облако** |
| Тип TG-аккаунта | **Личный** (MTProto/Telethon), не бот |
| **Мультиаккаунтность** | **У каждого менеджера свой TG-аккаунт (своя симка)**, ≤10 менеджеров |
| Стек | Python + Telethon + FastAPI + Postgres + Redis |
| Интеграция B24 | Локальное OAuth-приложение (даёт placement-виджет) |
| Чат в карточке | Встроенная вкладка-Placement (iFrame), вариант A |
| Хранение переписки | Своя БД + дублирование в timeline Bitrix24 |
| Инициация диалогов | Ответы на входящие + точечная из карточки. Массовые рассылки — этап 2 |
| Деплой | VPS на Linux (production-стиль) |

---

## 2. Архитектура: модульный монолит с провайдерами + разделение процессов

Выбран из 3 вариантов (отклонены: простой монолит — не расширяем под MAX; микросервисы — оверинжиниринг для задачи).

### 2.1. Разделение на два app-процесса (изоляция сбоев)

Приложение разделено на **2 процесса** (`web` и `bridge`) для изоляции нестабильного MTProto-компонента от стабильного HTTP-слоя. Полный деплой включает 5 контейнеров (см. §7.1: эти 2 + `nginx` + `postgres` + `redis`), но архитектурно значимое разделение — именно между `web` и `bridge`:

```
                  интернет
                     │
        ┌────────────┴────────────┐
        │  :443  Nginx            │  TLS, reverse proxy, статика Web UI
        └────────────┬────────────┘
                     │
       ┌─────────────┴──────────────┐
       ▼                            ▼
┌─────────────────────┐   /webhook/b24   ← события из Bitrix24
│   web (FastAPI)     │   /placement/card ← iFrame в карточке сделки
│   • REST API        │   /oauth/callback ← OAuth flow B24
│   • Web UI (iFrame) │   /health
│   • B24 OAuth mgr   │
│   • Bitrix24Sync    │
└──────────┬──────────┘
           │  Postgres + Redis (общие)
           │
┌──────────┴──────────────────────────────────────────────┐
│   bridge (asyncio процесс)                               │
│   • SessionManager (пул Telethon-сессий, по менеджерам)  │
│   • OutboxWorker (throttle на каждый аккаунт + retry)    │
│   • HealthChecker (каждая сессия раз в 5 мин)            │
└──────────┬───────────────────────────────────────────────┘
           │ MTProto (до 10 одновременных сессий)
           ▼
        TELEGRAM                              BITRIX24 (облако)
```

**Почему разделение:**
- Краш/переподключение одной TG-сессии не роняет веб-интерфейс для всех менеджеров
- Bridge можно рестартить (переподключение TG) без даунтайма UI
- `web` масштабируется горизонтально, если когда-то понадобится
- Bridge с пулом из 10 сессий — более тяжёлый/сбойный компонент, его изоляция оправдана

### 2.2. Ключевые абстракции (внутри кода)

| Абстракция | Ответственность |
|---|---|
| `MessengerProvider` (интерфейс) | `send_message()`, `on_message()`, `on_status()`. Реализации: `TelegramProvider` (Telethon), (позже) `MaxProvider`. **Точка расширения под MAX.** |
| `SessionManager` | Пул Telethon-клиентов (по одному на менеджера). Управляет жизненным циклом сессий: подключение, переподключение, health. |
| `Bitrix24Sync` | Только CRM: матчинг по номеру, авто-создание Контакта/Сделки, запись в timeline, уведомления. |
| `OutboxWorker` | Чтение очереди `outbox`, throttle, отправка через `MessengerProvider`, retry с backoff. |
| `HealthChecker` | Периодическая проверка каждой сессии (`is_connected`, авторизация, ошибки). |

---

## 3. Мультиаккаунтность (ключевая модель)

**Реальность бизнеса:** у каждого менеджера свой TG-аккаунт (своя симка). Все аккаунты подключены к Bitrix24. Это кардинально отличается от модели «один общий аккаунт с маршрутизацией».

### 3.1. Жёсткая привязка
```
1 менеджер = 1 TG-аккаунт = 1 b24_user_id
```
Это **снимает задачу маршрутизации диалогов**: аккаунт Ивана = диалог Ивана = сделка Ивана. Ответственный определяется автоматически по тому, на какой аккаунт пришло сообщение. Это и есть «userId, который Wazzup передаёт в Bitrix24» — у нас он implicit (вытекает из аккаунта-получателя).

### 3.2. Матчинг клиента с CRM
При входящем сообщении клиент идентифицируется по номеру телефона (доступно через MTProto user-API):
1. `crm.duplicate.findbyComm(PHONE, "+7999...")` — поиск существующего клиента
2. Найден → привязать диалог к существующей Сделке ответственного менеджера
3. Не найден → `crm.item.add` (Контакт + Сделка, `assigned_by_id = b24_user_id` менеджера)

### 3.3. Жизненный цикл менеджера
- **Добавление менеджера:** подключаем новый TG-аккаунт (код/QR + 2FA) → новая строка в `tg_accounts` + `managers` → bridge поднимает новую сессию
- **Увольнение менеджера:** аккаунт **остаётся у компании**, перепривязываем `manager_id` к новому менеджеру. История диалогов сохранена в CRM и в нашей БД.

---

## 4. Интеграция с Bitrix24

### 4.1. Локальное OAuth-приложение
Выбрано локальное OAuth-приложение (НЕ входящий вебхук), т.к. виджет в карточке сделки — ключевая фича.

| Характеристика | Значение |
|---|---|
| Тип авторизации | OAuth 2.0 (access_token + refresh_token) |
| Refresh | Автоматический, ~каждые 30 дней |
| Что даёт | placement handler (виджет в карточке), `event.bind`, чат-бот, REST от имени приложения |
| Требование | Права администратора B24 для установки |
| Скоупы | `crm`, `im`, `placement` |

Вебхук-стиль (`/rest/user/code/...`) не выбран, т.к. не даёт placement и event.bind.

### 4.2. Реальные методы B24 REST API (сверены через Bitrix24 MCP)

| Действие | Метод | Примечание |
|---|---|---|
| Создать Контакт/Сделку | `crm.item.add` | Универсальный (`entityTypeId`: 1=Лид, 2=Сделка, 3=Контакт). Старые `crm.deal.add`/`crm.contact.add` **устарели** |
| Запись переписки в timeline | `crm.timeline.comment.add` | `{ENTITY_TYPE:"deal", ENTITY_ID, COMMENT, FILES:[["name","base64"]]}` |
| Матчинг клиента по номеру | `crm.duplicate.findbyComm` | Поиск по телефону/email |
| Подписка на события CRM | `event.bind` | `{event:"onCrmDealAdd", handler:"https://домен/webhook/b24"}` |
| Уведомление менеджеру | `im.message.add` | Чат-бот Bitrix24 |
| Поиск сущностей | `crm.contact.list`, `crm.deal.list` | Доп. lookup |

### 4.3. Встраивание чата в карточку: Вариант A (Placement)
Вкладка-Placement — iFrame нашего Web UI прямо в карточке сделки. Менеджер не покидает карточку, повтор UX Wazzup. Bitrix24 открывает наш `/placement/card` URL, передавая контекст (ID сделки, ID пользователя). Вариант B (кнопка → отдельное окно) отклонён.

---

## 5. Модель данных (Postgres)

### 5.1. ER-схема

```
tg_accounts                       managers
┌───────────────────────────┐    ┌─────────────────────────┐
│ id            int PK      │    │ id          int PK       │
│ phone         text UQ     │◀───│ tg_account_id int FK 1:1 │
│ session_path  text        │    │ b24_user_id  int UQ ◀── матчинг с B24
│ status enum(active,       │    │ name         text        │
│        banned, offline)   │    │ role  enum(manager,..)   │
│ manager_id    int FK 1:1 ─│───▶│ is_active    bool        │
│ last_floodwait_at ts      │    │ created_at   timestamp   │
└───────────────────────────┘    └─────────────────────────┘

contacts                          dialogs
┌─────────────────────────────┐  ┌──────────────────────────────────┐
│ id            int PK        │  │ id                  int PK        │
│ tg_user_id    bigint UQ     │◀─│ contact_id          int FK        │
│ phone         text          │  │ messenger enum(tg, max)           │
│ username      text          │  │ external_chat_id    text          │
│ name          text          │  │ crm_deal_id         int NULL ◀───│
│ crm_contact_id int NULL ◀───│─▶│ crm_entity_type     text          │
│ created_at    timestamp     │  │ assigned_user_id    int NULL      │
│ updated_at    timestamp     │  │ title               text          │
└─────────────────────────────┘  │ status  enum(active, archived)   │
                                 │ last_msg_at         timestamp    │
                                 │ created_at          timestamp    │
                                 └──────────────────────────────────┘
                                          │ 1
                                          │
                                          │ N
                                          ▼
messages                          attachments
┌─────────────────────────────┐  ┌──────────────────────────────────┐
│ id            bigint PK     │  │ id            int PK             │
│ dialog_id     int FK        │  │ message_id    bigint FK          │
│ direction  enum(in, out)    │  │ type enum(photo,file,video,      │
│ tg_message_id bigint        │  │              voice, sticker)     │
│ text          text          │  │ file_path/blob_url text          │
│ status enum(sent, delivered,│  │ mime_type     text               │
│       read, error, pending) │  │ size          bigint             │
│ sent_at       timestamp     │  └──────────────────────────────────┘
│ author_user_id int NULL     │  (для out — кто из менеджеров)
│ timeline_comment_id int NULL│  ── привязка к комментарию в B24
│ created_at    timestamp     │
└─────────────────────────────┘

outbox (очередь отправки)         templates (быстрые ответы)
┌─────────────────────────────┐  ┌──────────────────────────────────┐
│ id            bigint PK     │  │ id            int PK             │
│ dialog_id     int FK        │  │ title         text               │
│ tg_account_id int FK        │  │ body          text               │
│ text / attachment_id        │  │ category      text               │
│ status enum(queued, sending,│  │ created_by    int FK             │
│         sent, failed,       │  │ created_at    timestamp          │
│         retrying)           │  └──────────────────────────────────┘
│ attempts      int           │
│ next_attempt_at timestamp   │
│ created_at    timestamp     │
└─────────────────────────────┘
```

### 5.2. Ключевые решения модели
1. **`contacts.tg_user_id`** — связь через Telegram ID (постоянный, в отличие от номера телефона, который может меняться).
2. **`dialogs`** — связующее звено: контакт ↔ мессенджер ↔ сделка CRM (`crm_deal_id`). Один контакт может иметь несколько диалогов (через TG, позже через MAX).
3. **`messages.timeline_comment_id`** — храним ID комментария в timeline Bitrix24. Позволяет обновлять/удалять комментарий при редактировании и избежать дублей.
4. **`outbox` отделён от `messages`** — throttle-воркер читает только pending-задачи, не сканируя всю переписку. Защита от бана изолирована.
5. **`messenger` enum уже сейчас** (`tg`, `max`) — готово под MAX без миграции схемы.
6. **`tg_accounts.manager_id` 1:1** — жёсткая привязка аккаунта к менеджеру. При увольнении — перепривязываем `manager_id`, аккаунт остаётся.

---

## 6. Anti-ban и надёжность MTProto

Личный аккаунт через MTProto — самый хрупкий элемент системы. >14 новых чатов по номеру за 3 мин → бан инициации на неделю. Отвечать на входящие — безопасно без лимитов.

### 6.1. Четыре слоя защиты

| Слой | Механизм |
|---|---|
| **1. Throttler (token bucket)** | Раздельные очереди на каждый аккаунт: «ответы» (мягкий лимит ~20/мин) и «инициация» (жёсткий — 10 за 3 мин, запас от красной линии 14, мин 5 сек между). Плюс глобальный rate limiter TG API. |
| **2. OutboxWorker с retry+backoff** | `FloodWait` → ждём сколько просит TG. `UserBanned` → алерт админу + пауза инициаций. Сеть/таймаут → экспоненциальный backoff (30с, 2м, 8м). Max 5 попыток → `failed` + уведомление менеджеру. |
| **3. HealthChecker** | Раз в 5 мин на каждую сессию: `client.is_connected()`, авторизация, наличие `FloodWait`/`AuthKeyError` → алерт админу. |
| **4. Защита .session** | Persistent volume, бэкап, шифрование at rest. Один процесс = одна сессия (параллельный запуск → TG кикнет одну). |

### 6.2. State machine сессии (на каждый аккаунт)
```
  ACTIVE ──FloodWait/признаки бана──▶ THROTTLED ──таймаут прошёл──▶ ACTIVE
    │                                     │
    │               SpamBot-блок          │
    └─────────────────────────────────────▶ BANNED ──алерт админу──▶ MANUAL
```
**Критично: в бане приём входящих работает — отвечать можно всегда. Блокируется только инициация новых диалогов по номеру.** Система не теряет функциональность при бане, только временно отключает рассылки.

---

## 7. Деплой и эксплуатация

### 7.1. Топология (один VPS, 5 Docker-контейнеров)
- **OS:** Ubuntu 22.04 LTS
- **Ресурсы на старте:** 2 vCPU / 4 GB RAM / 40 GB SSD (Telethon лёгкий, основная память — пул ≤10 сессий ~500МБ–1ГБ + БД + web)
- **Контейнеры:**
  - `nginx` — TLS (Let's Encrypt), reverse proxy, статика Web UI
  - `web` — FastAPI (HTTP, REST API, placement-виджет, Web UI, Bitrix24Sync)
  - `bridge` — пул Telethon-сессий + OutboxWorker + HealthChecker
  - `postgres` — БД (+volume)
  - `redis` — очередь outbox + кэш throttler + rate-limit + pub/sub для WebSocket

### 7.2. Persistent volumes
`pg_data/`, `tg_sessions/` (⚠ ключи к аккаунтам, до 10 файлов), `attachments/`, `certs/`

### 7.3. Первый вход TG-аккаунта (разовый, на каждый аккаунт)
CLI-команда на сервере:
```
docker compose run --rm bridge python -m app.auth_login --phone +7...
```
Ввод кода из TG → пароль 2FA → `.session` сохраняется в volume. Далее сессия живёт в volume, `restart: always`.

### 7.4. Конфигурация (.env)
```
# Telegram (MTProto)
TG_API_ID=...               # с my.telegram.org
TG_API_HASH=...
TG_SESSIONS_DIR=/data/tg_sessions

# Bitrix24 OAuth
B24_PORTAL=https://xxx.bitrix24.ru
B24_CLIENT_ID=...
B24_CLIENT_SECRET=...
B24_OAUTH_REDIRECT=https://домен/oauth/callback
B24_WEBHOOK_SECRET=...      # проверка подписи входящих вебхуков

# Throttling (защита от бана)
THROTTLE_INIT_MAX=10        # инициаций за окно
THROTTLE_INIT_WINDOW=180    # окно в сек (3 мин)
THROTTLE_INIT_MIN_INTERVAL=5

# Инфра
DATABASE_URL=postgresql+asyncpg://...
REDIS_URL=redis://redis:6379/0
SENTRY_DSN=...
```

### 7.5. Мониторинг и алерты
- **Health endpoint** `/health`: статус всех сессий, БД, Redis → Nginx/uptime-чекам
- **Sentry** — все исключения приложения
- **Критичные алерты админу** (в Telegram/email): сессия упала / `.session` невалиден / FloodWait > 1ч / аккаунт в бане / OAuth Bitrix24 требует re-authorize

---

## 8. Потоки данных

### 8.1. Входящее сообщение (мультиаккаунт)
1. Клиент (+7999...) пишет в TG
2. Сообщение приходит на аккаунт #1 (Иванова, `.session 1`)
3. `SessionManager` определяет: `manager_id=1` → `b24_user_id=15`
4. Сохраняем сообщение в Postgres (`messages` + `dialogs` + `contacts`)
5. `Bitrix24Sync`: `crm.duplicate.findbyComm(PHONE, "+7999...")`
   - Найден → привязать диалог к существующей Сделке
   - Не найден → `crm.item.add` (Контакт + Сделка, `assigned_by_id=15`)
6. `crm.timeline.comment.add` → текст сообщения в timeline сделки
7. `im.message.add` → уведомление Ивану в чат-бот Bitrix24
8. Push в Web UI (Redis pub/sub → web → WebSocket менеджеру)

### 8.2. Исходящее сообщение (менеджер отвечает)
1. Менеджер пишет в Web UI / placement-виджете карточки сделки
2. `web` сохраняет в `messages` (direction=out, status=pending) + создаёт запись в `outbox`
3. `OutboxWorker` (bridge) забирает задачу → throttle-проверка → `TelegramProvider.send_message()` через нужную сессию менеджера
4. Telethon отправляет в MTProto → получаем `tg_message_id`
5. Обновляем `messages.status` (sent → delivered → read), статусы приходят через MTProto events
6. Записываем в timeline Bitrix24

---

## 9. Готовность к расширению (MAX)

Архитектура спроектирована под добавление мессенджера MAX на втором этапе:
- `MessengerProvider` — интерфейс; `TelegramProvider` уже реализует его, `MaxProvider` добавится без изменения `Bitrix24Sync`/`OutboxWorker`
- `dialogs.messenger` enum уже содержит `max`
- `SessionManager` абстрагирует пул сессий — MAX-сессии добавятся параллельно TG

---

## 10. Sustainability-анализ

FastAPI — **не узкое место** системы. ~5 000–25 000 RPS/ядро при реальной нагрузке <50 RPS → запас x100. Postgres/Redis — аналогично с огромным запасом.

**Реальный потолок системы = сам TG-аккаунт (Telethon/MTProto), ~30 сооб/сек на аккаунт.** С ≤10 аккаунтами совокупная пропускная способность ~300 сооб/сек, что с большим запасом покрывает бизнес-потребности отдела продаж.

Главная мера стабильности — **изоляция сбоев MTProto** через разделение `web` + `bridge` и per-account state machine (сессия в бане не блокирует приём входящих).

---

## 11. Открытые вопросы (уточнить при реализации)

1. **MAX API:** на момент спецификации детали API MAX не исследовались. Структура `MessengerProvider` готова, но реализация `MaxProvider` потребует отдельного исследования протокола MAX.
2. **Роботы/Бизнес-процессы Bitrix24:** действие «Отправить сообщение» в Роботах/БП B24 — описано в Wazzup, но в scope текущей спецификации как отдельныйplacement-handler не входит. Может быть добавлено как REST-эндпоинт, вызываемый из activity-обработчика B24.
3. **Массовые рассылки (этап 2):** требуют отдельного UI и строгого throttle-планировщика (учёт окон по каждому аккаунту, распределение во времени). Outbox-модель уже поддерживает эту возможность.
