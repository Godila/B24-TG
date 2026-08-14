# Plan 007: UX-патчи — новейшая история в чате, vendored Alpine, media-placeholder

> **Executor instructions**: три независимых патча, каждый со своей Verify. STOP — стоп и доклад. Обнови строку в `plans/README.md`.
>
> **Drift check**: `git diff --stat 24a661e..HEAD -- src/app/web/routes/dialogs.py src/app/static/ src/app/messaging/telegram/provider.py tests/integration/test_dialogs_api.py`. Расхождение = STOP.

## Status
- **Priority**: P2 | **Effort**: S-M | **Risk**: LOW
- **Depends on**: none
- **Category**: bug / ux
- **Planned at**: commit `24a661e`, 2026-08-14

## Why this matters

Три UX-дефекта виджета: (1) история грузится с САМЫХ СТАРЫХ N сообщений (`order_by(id.asc()).limit(50)`) — в длинном диалоге менеджер видит древнюю историю, а live-хвост догоняет по 50 сообщений каждые 3 секунды; (2) Alpine.js подключён с unpkg.com — на РФ-сетях/корпоративных фильтрах CDN часто недоступен, и виджет молча рендерится сырым `x-data`-мусором у ВСЕХ менеджеров; (3) фото/войс/стикер от клиента превращаются в пустой пузырь и пустой комментарий в CRM (`content_type` захардкожен `text`, текст пуст) — для TG-продукта это рутина, и молчаливая потеря недопустима (полноценные вложения — отдельная тема; здесь честный placeholder).

## Current state

1. `src/app/web/routes/dialogs.py:107-112`:
```python
stmt = select(Message).where(Message.dialog_id == dialog_id)
if since is not None:
    stmt = stmt.where(Message.id > since)
stmt = stmt.order_by(Message.id.asc()).limit(limit)
```
2. `src/app/static/placement.html:9`: `<script defer src="https://unpkg.com/alpinejs@3.x.x/dist/cdn.min.js"></script>`. В `src/app/static/` только `app.js`, `placement.html`, `style.css`.
3. `src/app/static/app.js:69-79` (`loadMessages`): `fetch(.../messages?limit=100)`; далее `poll()` с `since=lastId`. Рендер — `x-for` по `messages` (ascending).
4. `src/app/messaging/telegram/provider.py:63-68`: `content_type=ContentType.text` всегда; `text=event.message.message`.
5. `src/app/messaging/types.py`: `ContentType(text/photo/file/video/voice/sticker)`.
6. Тесты: `tests/integration/test_dialogs_api.py` (GET messages), `tests/unit/test_telegram_provider.py` (образец; в плане 002 туда добавлены тесты `_on_new_message` — опирайся на них).

## Commands

| Purpose | Command | Expected |
|---|---|---|
| Тесты | `.venv/Scripts/python.exe -m pytest -q` | all pass |
| Линт | `.venv/Scripts/ruff.exe check src/ tests/` | exit 0 |
| curl Alpine | `curl -fsSL -o src/app/static/vendor/alpine.min.js https://unpkg.com/alpinejs@3.14.9/dist/cdn.min.js` | файл ~44KB (minified) |

## Scope

**In scope**: `src/app/web/routes/dialogs.py`, `src/app/static/placement.html`, `src/app/static/app.js`, `src/app/static/vendor/alpine.min.js` (новый), `src/app/messaging/telegram/provider.py`, `tests/integration/test_dialogs_api.py`, `tests/unit/test_telegram_provider.py`.

**Out of scope**: полноценный приём/отправка вложений (Attachment-модель, скачивание медиа, UI-превью) — отдельная фича; `status_stream`; `style.css` (кроме случая, когда placeholder потребует класса — минимально).

## Git workflow
`main`, коммиты: `fix(api): newest-first message history + before cursor`, `fix(ui): vendor Alpine.js locally`, `fix(telegram): media placeholders instead of silent drop`.

## Steps

### Patch A: Newest-first история + курсор `before_id`

1. `dialogs.py` `list_messages`: новый query-параметр `before: int | None` и семантика:
   - если задан `since` → как сейчас (asc, poll-режим, контракт не менять);
   - если `before` задан → `Message.id < before`, `order_by(id.desc()).limit(limit)` → вернуть DESC (клиент сам реверснет) — страница «назад»;
   - иначе (первичная загрузка) → `order_by(id.desc()).limit(limit)` → DESC (новейшие N).
   Ответ дополнить полем? НЕТ — не менять форму `list[MessageOut]`; UI реверсит сам.
2. `app.js/loadMessages`: после fetch — `this.messages = data.reverse()` (в asc для рендера); `this.lastId = Math.max(...)`. Новый метод `loadOlder()`: `before = this.messages[0].id` → fetch DESC → `this.messages = [...data.reverse(), ...this.messages]`; кнопка «↑ Загрузить ещё» в `placement.html` над лентой (`x-show="messages.length > 0 && hasMore"`, `hasMore = data.length === limit`).
3. Тесты (`test_dialogs_api.py`): seed 5 сообщений; (a) без параметров → DESC (id 5..4 при limit=2), (b) `before=<id третьего>` → старее, (c) `since` — как раньше (asc).

**Verify**: `pytest tests/integration/test_dialogs_api.py -q` → pass.

### Patch B: Vendor Alpine.js

1. `mkdir -p src/app/static/vendor && curl -fsSL -o src/app/static/vendor/alpine.min.js https://unpkg.com/alpinejs@3.14.9/dist/cdn.min.js` (фиксированная версия). Проверь размер > 30KB и что файл начинается с комментария/minified JS (не HTML-заглушка CDN).
2. `placement.html:9` → `<script defer src="/static/vendor/alpine.min.js"></script>`.
3. Проверка что не осталось внешних источников: `grep -n "unpkg\|cdn\." src/app/static/*.html` → пусто.

**Verify**: `curl` выше ok; grep пусто; `pytest -q` green (статика тестами не покрыта — smoke: `pytest tests/integration/test_app_wiring.py -q`, там StaticFiles).

### Patch C: Media-placeholder

`provider.py/_on_new_message` — определить тип и текст-заглушку:
```python
def _content_type_and_text(message) -> tuple[ContentType, str | None]:
    text = message.message or None
    media = getattr(message, "media", None)
    if media is None:
        return ContentType.text, text
    # у медиа-сообщений text = подпись (caption), может быть None
    import telethon.tl.types as tl
    if isinstance(media, tl.MessageMediaPhoto):
        return ContentType.photo, text or "[фото]"
    if isinstance(media, tl.MessageMediaDocument):
        doc = media.document
        attrs = getattr(doc, "attributes", [])
        names = {type(a).__name__ for a in attrs}
        if "DocumentAttributeAudio" in names:
            return ContentType.voice, text or "[голосовое сообщение]"
        if "DocumentAttributeVideo" in names:
            return ContentType.video, text or "[видео]"
        if "DocumentAttributeSticker" in names:
            return ContentType.sticker, text or "[стикер]"
        return ContentType.file, text or "[файл]"
    return ContentType.file, text or "[вложение]"
```
(helpers-модуль или staticmethod класса; импорт `telethon.tl.types as tl` наверху). В `_on_new_message`: `ctype, text = self._content_type_and_text(event.message)` → использовать. Тест: расширить тест из плана 002 — event с `message.media = SimpleNamespace()`-заглушкой не сработает для isinstance — вместо этого тестируй `_content_type_and_text` напрямую с реальными `telethon.tl.types.MessageMediaPhoto` (конструируются: `MessageMediaPhoto` требует fields — используй `SimpleNamespace` НЕ выйдет; вместо isinstance-теста — патч-подход: мини-фабрика. Если конструирование TL-объектов в тесте громоздко — тестируй ветку `media is None` (text) и добавь интеграционную проверку при e2e. Минимум: тест, что `media=None + text` → (text, text), и что caption у фото сохраняется: сконструируй `tl.MessageMediaPhoto(photo=None)` — допустимо, telethon TL-объекты позволяют keyword-инициализацию с дефолтами).

**Verify**: `pytest tests/unit/test_telegram_provider.py -q` → pass; полный `pytest -q` green; `ruff check` exit 0.

## Test plan
A: 3 теста API. B: grep + существующий wiring-тест. C: 2-3 теста `_content_type_and_text` (text без media; photo без caption → "[фото]"; photo с caption → caption). Полный прогон.

## Done criteria
- [ ] `pytest -q` green; `ruff check` exit 0
- [ ] `curl -s "https://b24-tg.haragy.top/api/dialogs/1/messages" ...` — оператор: DESC-порядок (после деплоя)
- [ ] `grep -rn "unpkg" src/app/static/` — пусто; `vendor/alpine.min.js` в репо
- [ ] `grep -n "фото\]" src/app/messaging/telegram/provider.py` — placeholder есть
- [ ] `plans/README.md`: 007 → DONE

## STOP conditions
- DESC-изменение ломает `app.js`-poll (двойной рендер, дубли) — проверь вручную логику `since`-режима: он остался ASC; если UI смешал — исправь минимально, не_redesign.
- telethon TL-типы в тесте не конструируются разумно — сделай только `media is None`-тест + пометь e2e-проверку в maintenance, не выдумывай моки дальше.
- unpkg недоступен из сети разработчика — возьми зеркало `https://cdn.jsdelivr.net/npm/alpinejs@3.14.9/dist/cdn.min.js` и зафиксируй источник комментарием в README-dev.

## Maintenance notes
- Полные вложения (скачивание, превью, Attachment-записи) — следующая итерация; placeholder — честный минимум. `ContentType` уже проставляется правильно — UI и CRM-комментарии получат осмысленный текст сразу.
- `vendor/alpine.min.js` — обновлять осознанно (pin версии в комментарии к строке script).
- `before`-курсор: если появится пагинация в списке диалогов — тот же паттерн.
