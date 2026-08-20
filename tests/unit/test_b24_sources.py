"""sources: fail-closed парсер справочника + поиск похожих по имени."""

from unittest.mock import AsyncMock

import pytest

from app.b24.sources import B24Source, _parse_source, fetch_sources, name_looks_like
from app.models import Messenger


def test_parse_source_ok_and_fail_closed():
    assert _parse_source({"STATUS_ID": "TELEGRAM", "NAME": "Telegram (мессенджер)"}) == (
        B24Source(status_id="TELEGRAM", name="Telegram (мессенджер)")
    )
    assert _parse_source("мусор") is None
    assert _parse_source({"NAME": "без кода"}) is None
    assert _parse_source({"STATUS_ID": "X", "NAME": None}) is None


@pytest.mark.asyncio
async def test_fetch_sources_tolerant_unwrap():
    """crm.status.list отвечает и списком, и {"items": [...]} — мусор мимо."""
    client = AsyncMock()
    client.call = AsyncMock(
        return_value=[{"STATUS_ID": "CALL", "NAME": "Звонок"}, "мусор"]
    )
    assert await fetch_sources(client, "t") == [B24Source(status_id="CALL", name="Звонок")]

    client.call = AsyncMock(return_value={"items": [{"STATUS_ID": "WEB", "NAME": "Сайт"}]})
    assert await fetch_sources(client, "t") == [B24Source(status_id="WEB", name="Сайт")]


def test_name_looks_like_word_boundary():
    assert name_looks_like("Telegram (мессенджер)", Messenger.tg) is True
    assert name_looks_like("Написать в ТГ", Messenger.tg) is True
    assert name_looks_like("MAX (мессенджер)", Messenger.max) is True
    # Граница слова: «Максим» — не MAX (иначе подсветка врала бы на ФИО).
    assert name_looks_like("Максим Никифоров", Messenger.max) is False
    assert name_looks_like("Звонок", Messenger.tg) is False
