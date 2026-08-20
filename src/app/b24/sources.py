"""Справочник CRM-источников портала (crm.status, ENTITY=SOURCE).

Источник карточки для канала задаётся панелью (app_settings.source_map);
этот модуль — чтение справочника и поиск «похожих по имени» записей:
NAME в B24 неуникален, и без подсветки похожих легко завести дубль
(админ руками создал «Telegram», мы добавили свой «TELEGRAM»).
"""

import logging
import re
from dataclasses import dataclass

from app.b24.client import Bitrix24Client
from app.models import Messenger

logger = logging.getLogger(__name__)

#: Код записи справочника (STATUS_ID): латиница/цифры/подчёркивание, ≤32.
#: Единственный источник истины для валидации и в репозитории, и в API.
SOURCE_ID_RE = r"[A-Za-z0-9_]{0,32}"


@dataclass(frozen=True, slots=True)
class B24Source:
    """Запись справочника источников."""

    status_id: str
    name: str


def _parse_source(row: object) -> B24Source | None:
    """Разбор строки crm.status.list (fail-closed: мусор — warning + skip)."""
    if not isinstance(row, dict):
        logger.warning("sources: не-словарная строка справочника пропущена: %r", row)
        return None
    status_id = row.get("STATUS_ID")
    name = row.get("NAME")
    if not isinstance(status_id, str) or not status_id or not isinstance(name, str):
        logger.warning("sources: строка без STATUS_ID/NAME пропущена: %r", row)
        return None
    return B24Source(status_id=status_id, name=name)


async def fetch_sources(client: Bitrix24Client, auth_token: str) -> list[B24Source]:
    """Все записи справочника источников портала (один вызов)."""
    result = await client.call(
        "crm.status.list",
        auth_token=auth_token,
        params={"filter": {"ENTITY_ID": "SOURCE"}},
    )
    if isinstance(result, dict):
        result = result.get("items", [])
    rows = result if isinstance(result, list) else []
    # ponytail: без пагинации crm.status.list — добавить при справочнике >50 записей
    parsed = (_parse_source(r) for r in rows)
    return [s for s in parsed if s is not None]


#: «Похоже на канал» — граница слова, без регистра: «Максим» ≠ MAX,
#: «Telegram (мессенджер)» = telegram.
_SOURCE_NAME_TOKENS: dict[Messenger, re.Pattern[str]] = {
    Messenger.tg: re.compile(r"\b(telegram|tg|телеграм|тг)\b", re.IGNORECASE),
    Messenger.max: re.compile(r"\b(max|макс)\b", re.IGNORECASE),
}


def name_looks_like(name: str, messenger: Messenger) -> bool:
    return bool(_SOURCE_NAME_TOKENS[messenger].search(name or ""))
