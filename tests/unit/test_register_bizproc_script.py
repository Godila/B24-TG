"""Смоук скрипта регистрации активити БП (один assert-чек, паттерн scripts/)."""

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "register_bizproc.py"


def _load():
    spec = importlib.util.spec_from_file_location("register_bizproc", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_activity_constants():
    """Load-bearing контракт регистрации: fire-and-forget семантика хендлера
    держится на USE_SUBSCRIPTION=N, Required message и нашем HANDLER."""
    mod = _load()
    act = mod.ACTIVITY
    assert act["USE_SUBSCRIPTION"] == "N"
    assert act["HANDLER"].endswith("/webhook/b24/bizproc")
    assert act["PROPERTIES"]["message"]["Required"] == "Y"
