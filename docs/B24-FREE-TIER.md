# B24 Free Tier: доступность CRM-методов (Plan 003)

> Шаблон. Заполнить после фазы B — запуска
> `docker compose exec web python /app/scripts/verify_b24_methods.py` на prod VM.
> Варианты решений: **OK** / **платный тариф** / **деградация** (какая фича отключается).

## Параметры прогона

- Дата запуска: _YYYY-MM-DD_
- Коммит на VM: _`git rev-parse --short HEAD`_
- Exit code скрипта: _0 / 1 / 2_
- Флаги: _`--skip-im` если использовался_

## Результаты по методам

| # | Метод | Статус (OK / ERROR / FATAL) | Решение | Комментарий |
|---|---|---|---|---|
| 1 | `app.info` |  |  |  |
| 2 | `crm.duplicate.findbycomm` |  |  |  |
| 3 | `crm.item.add` (contact, entityTypeId=3) |  |  |  |
| 4 | `crm.item.add` (deal, entityTypeId=2) |  |  |  |
| 5 | `crm.timeline.comment.add` |  |  |  |
| 6 | `im.message.add` |  |  |  |
| 7 | `crm.item.delete` (cleanup: deal + contact) |  |  |  |

## Итоговое решение по тарифу

_(OK — остаёмся на free / нужен платный тариф — какой / деградация — какие фичи отключаем)_
