"""add_max_source: дубль-гард — похожая по имени запись с другим кодом
(скрипты — один assert-чек по AGENTS.md)."""

import importlib.util
from pathlib import Path

from app.b24.sources import B24Source


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "add_max_source", Path(__file__).parents[2] / "scripts" / "add_max_source.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_duplicate_hint():
    mod = _load_module()
    similar = [B24Source("TGRAM", "Telegram (мессенджер)")]
    assert mod.duplicate_hint(similar, "TELEGRAM") is not None  # создали бы дубль
    assert mod.duplicate_hint(similar, "MAX") is None  # чужое имя не мешает
    assert mod.duplicate_hint(
        [B24Source("TELEGRAM", "Telegram (мессенджер)")], "TELEGRAM"
    ) is None  # наш же код — не дубль
