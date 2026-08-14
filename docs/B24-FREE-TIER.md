# B24 Free Tier: доступность CRM-методов (Plan 003)

> Шаблон. Заполнить после фазы B — запуска
> `docker compose exec web python /app/scripts/verify_b24_methods.py` на prod VM.
> Варианты решений: **OK** / **платный тариф** / **деградация** (какая фича отключается).

## Параметры прогона

- Дата запуска: 2026-08-14
- Коммит на VM: `edddace`
- Exit code скрипта: 0
- Флаги: без флагов (полный прогон, включая im)

## Результаты по методам

| # | Метод | Статус (OK / ERROR / FATAL) | Решение | Комментарий |
|---|---|---|---|---|
| 1 | `app.info` | OK | OK | Токен жив, приложение активно |
| 2 | `crm.duplicate.findbycomm` | OK | OK | Пустой результат = «не найдено», это норма |
| 3 | `crm.item.add` (contact, entityTypeId=3) | OK | OK | После фикса клиента (см. ниже) |
| 4 | `crm.item.add` (deal, entityTypeId=2) | OK | OK | CONTACT_ID связка работает |
| 5 | `crm.timeline.comment.add` | OK | OK | Killer feature доступна |
| 6 | `im.message.add` | OK | OK | Первая попытка дала ERROR_NO_ACCESS (транзиентно), повтор — OK |
| 7 | `crm.item.delete` (cleanup: deal + contact) | OK | OK | Скрипт чистит за собой |

## Найденные и исправленные баги клиента (итог спайка)

Спайк дважды поймал баг `Bitrix24Client.call` до подключения реального номера:

1. Form-кодирование превращало `fields` (dict) в python-repr строку →
   error 100 на `crm.item.add`. Фикс: JSON-кодирование значений (промежуточный).
2. JSON-строка в form-поле тоже отвергалась `crm.item.add` (error 100).
   Финальный фикс: весь запрос уходит как `application/json` body с нативными
   объектами; form-идиома `values[]` заменена на `values` для findbycomm.
   Коммиты: `5bc5d37`, `edddace`. Тесты: `tests/unit/test_b24_client.py`.

Без спайка это всплыло бы первым же входящим сообщением на проде.

## Итоговое решение по тарифу

**OK — остаёмся на free-тарифе.** Ни один метод CRM-пути не вернул
`ACCESS_DENIED: REST API is available only on commercial plans`.
Ограничения free-tier остаются теоретическими по лимитам запросов
(2 rps, всплески до 50) — их закрывает throttle/retry в плане 006.

Примечание: первый прогон `im.message.add` (DIALOG_ID=1, админ) дал
`ERROR_NO_ACCESS`, повтор через ~10 минут — OK. Наблюдать; если
повторится на постоянной основе — смотреть настройки приватности чата
админа, а не тариф.
