# Plan 002: Починить входящий конвейер — Telethon NewMessage(incoming=True) + тесты шва

> **Executor instructions**: шаг за шагом, каждая Verify — команда с ожидаемым результатом. STOP-условие — стоп и доклад. По завершении обнови строку в `plans/README.md`.
>
> **Drift check**: `git diff --stat 24a661e..HEAD -- src/app/messaging/telegram/provider.py tests/unit/test_telegram_provider.py`. Изменились — сверь с «Current state»; расхождение = STOP.

## Status
- **Priority**: P1 | **Effort**: S | **Risk**: LOW
- **Depends on**: none
- **Category**: bug + tests
- **Planned at**: commit `24a661e`, 2026-08-14

## Why this matters

Входящий конвейер продукта мёртв одной строкой. `TelegramProvider.connect()` регистрирует обработчик `add_event_handler(self._on_new_message)` **без event-builder'а**. Сверено с исходником установленного Telethon (`.venv/Lib/site-packages/telethon/client/updates.py:182-186`): falsy `event` → `events.Raw()` → колбэк получает сырые TL `Update`-объекты «без обработки». `_on_new_message` сразу вызывает `await event.get_sender()` — у сырого Update такого метода нет → `AttributeError` на первом же апдейте → исключение глотается (`except Exception: logger.exception` рядом) → **ни одно входящее сообщение никогда не попадает в очередь, CRM-синхронизация и виджет не получают ничего**. Второй дефект той же строки: даже с наивным `events.NewMessage()` хендлер ловит и ИСХОДЯЩИЕ сообщения — собственные ответы менеджера из Telegram-приложения эхом пойдут как «входящие» (дубли, лишние контакты, двойная CRM-синхронизация). Тесты этого не видели: `test_telegram_provider.py` покрывает только `send_message`.

## Current state

`src/app/messaging/telegram/provider.py` (ключевые места):
```python
# :45 (в connect(), после is_user_authorized)
self._client.add_event_handler(self._on_new_message)   # ← БЕЗ builder → Raw

# :53-72
async def _on_new_message(self, event) -> None:
    try:
        sender = await event.get_sender()
        msg = IncomingMessage(
            account_id=0,  # SessionManager проставит реальный account_id (проставляет bootstrap.forward_incoming)
            external_chat_id=str(event.chat_id),
            sender_tg_id=getattr(sender, "id", 0),
            ...
            is_reply=bool(event.is_reply),
        )
        await self._incoming_queue.put(msg)
    except Exception:
        logger.exception("Failed to handle incoming TG message")

# :81-83
async def incoming_stream(self) -> AsyncIterator[IncomingMessage]:
    while True:
        yield await self._incoming_queue.get()
```
Импорты вверху файла: `from telethon import TelegramClient` и `from telethon.tl.types import User`. Класс `TelegramProvider.__init__(self, api_id, api_hash, sessions_dir)`, поле `self._client`.

`tests/unit/test_telegram_provider.py` — 2 теста, оба про `send_message` с моком `TelegramClient` (патчат `app.messaging.telegram.provider.TelegramClient`). Структурный образец для новых тестов — этот же файл.

Известный контракт дальше по цепочке: `app/bridge/bootstrap.py:forward_incoming` перезаписывает `msg.account_id = account.id` и зовёт `handler.handle(msg, account=account)` — там ничего менять не надо.

## Commands

| Purpose | Command | Expected |
|---|---|---|
| Тесты (target) | `.venv/Scripts/python.exe -m pytest tests/unit/test_telegram_provider.py -v` | pass |
| Полный suite | `.venv/Scripts/python.exe -m pytest -q` | all pass |
| Линт | `.venv/Scripts/ruff.exe check src/ tests/` | exit 0 |

## Scope

**In scope**: `src/app/messaging/telegram/provider.py`, `tests/unit/test_telegram_provider.py`.

**Out of scope**: `bootstrap.py`, `incoming_handler.py`, `session_manager.py` (их контракты не меняются); `status_stream` (мёртвый код — отдельная тема, план 005/006); CLI `auth.py`.

## Git workflow
`main`, коммит: `fix(telegram): register NewMessage(incoming=True) handler — inbound pipeline was dead`.

## Steps

### Step 1: Красный тест — регистрация с правильным builder

В `tests/unit/test_telegram_provider.py` добавь (в стиле существующих — мок `TelegramClient` через `unittest.mock.patch`):

```python
def test_connect_registers_newmessage_incoming_builder():
    """connect() обязан регистрировать events.NewMessage(incoming=True):
    без builder Telethon передаёт сырые Update — inbound мёртв (баг)."""
    with patch("app.messaging.telegram.provider.TelegramClient") as mock_tl:
        client_inst = AsyncMock()
        client_inst.is_user_authorized = AsyncMock(return_value=True)
        mock_tl.return_value = client_inst

        provider = TelegramProvider(api_id=1, api_hash="x", sessions_dir="/tmp/s")
        asyncio.run(provider.connect())

        client_inst.add_event_handler.assert_called_once()
        handler_arg, builder_arg = client_inst.add_event_handler.call_args[0]
        assert handler_arg == provider._on_new_message
        assert isinstance(builder_arg, events.NewMessage)
        # incoming=True: исходящие (свои) сообщения фильтруются.
        assert builder_arg.incoming is True
```
(импорты: `import asyncio`, `from unittest.mock import AsyncMock, patch`, `from telethon import events`.)

Прогони — должен упасть (сейчас builder отсутствует → распаковка 1 аргумента → ошибка). **Verify**: `pytest tests/unit/test_telegram_provider.py -v` → 1 failed именно на новом тесте.

### Step 2: Красный тест — `_on_new_message` принимает NewMessage-shaped event

```python
def test_on_new_message_builds_incoming_message():
    provider = TelegramProvider(api_id=1, api_hash="x", sessions_dir="/tmp/s")

    sender = SimpleNamespace(id=4242, first_name="Иван", last_name=None,
                             phone="+79990000000", username="ivan")
    event = SimpleNamespace(
        chat_id=4242,
        is_reply=False,
        message=SimpleNamespace(message="Привет", id=777, date=None),
        get_sender=AsyncMock(return_value=sender),
    )

    asyncio.run(provider._on_new_message(event))
    msg = asyncio.run(provider._incoming_queue.get())  # queue.get_nowait тоже ок
    assert msg.sender_tg_id == 4242
    assert msg.text == "Привет"
    assert msg.external_message_id == 777
    assert msg.external_chat_id == "4242"
    assert msg.account_id == 0  # перезапишет bootstrap.forward_incoming
```
Этот тест, скорее всего, ЗЕЛЁНЫЙ уже сейчас (логика `_on_new_message` корректна, сломана была регистрация) — тогда он выполняет роль characterization-теста. Если он красный из-за деталей — не меняй прод-код под него, разберись (STOP если непонятно).

**Verify**: `pytest tests/unit/test_telegram_provider.py -v` → статус понятен (1 failed от Step 1, Step 2 зелёный или с понятной причиной).

### Step 3: Фикс — одна строка + импорт

`src/app/messaging/telegram/provider.py`:
1. Импорты: `from telethon import TelegramClient, events`.
2. Строка `self._client.add_event_handler(self._on_new_message)` → 
```python
# incoming=True: только входящие. Без builder Telethon отдаёт сырые Update,
# а без фильтра исходящие сообщения менеджера эхом шли бы как входящие.
self._client.add_event_handler(
    self._on_new_message, events.NewMessage(incoming=True)
)
```

**Verify**: `.venv/Scripts/python.exe -m pytest tests/unit/test_telegram_provider.py -v` → все pass (2 старых + 2 новых).

### Step 4: Полный прогон + коммит

**Verify**: `.venv/Scripts/python.exe -m pytest -q` → 88 passed (86 + 2); `.venv/Scripts/ruff.exe check src/ tests/` → exit 0. `git status` — только 2 in-scope файла. Коммит.

## Test plan
Покрыто: (1) регистрация handler с `events.NewMessage(incoming=True)` — регрессия главного бага; (2) трансформация event → IncomingMessage (characterization). Глубже (реальный Telethon event-объект) — в e2e при подключении номера; здесь достаточно shape-стаба.

## Done criteria
- [ ] `pytest -q` green (88 passed)
- [ ] `grep -n "add_event_handler" src/app/messaging/telegram/provider.py` показывает `events.NewMessage(incoming=True)`
- [ ] `ruff check` exit 0
- [ ] Только 2 файла изменены
- [ ] `plans/README.md`: 002 → DONE

## STOP conditions
- `provider.py` не соответствует excerpt (уже кто-то правил).
- `events.NewMessage` не принимает `incoming` kwarg в установленной версии Telethon (проверь `python -c "from telethon import events; import inspect; print(inspect.signature(events.NewMessage.__init__))"` — должен содержать `incoming`). Нет параметра → STOP и доложи версию Telethon.
- Новый тест Step 1 не краснеет (значит, регистрация уже исправлена кем-то) — STOP, сверься.

## Maintenance notes
- Эхо-фильтр опирается на `incoming=True` Telethon; если появится второй способ отправки из этого же клиента (например, отправка напрямую через TG-приложение и через наш провайдер) — фильтр остаётся корректным (наши отправки = outgoing).
- После подключения реального номера: в bridge-логах НЕ должно быть `Failed to handle incoming TG message` на каждый апдейт; если есть — значит фильтр/shape всё ещё неверен (живой NewMessage.Event имеет `.message` как Message-объект с `.message`-строкой — стаб в Step 2 это отражает).
- Этот план — предпосылка для e2e: до него подключать номер бессмысленно.
