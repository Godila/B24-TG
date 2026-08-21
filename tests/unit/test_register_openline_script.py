"""Константы скрипта регистрации коннектора открытых линий."""

import importlib.util
from pathlib import Path


def _load():
    path = Path(__file__).resolve().parents[2] / "scripts" / "register_openline.py"
    spec = importlib.util.spec_from_file_location("register_openline", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_register_constants():
    mod = _load()
    assert mod.REGISTER["ID"] == "chatmost"
    assert mod.REGISTER["PLACEMENT_HANDLER"].endswith("/placement/connector")
    assert mod.REGISTER["NEED_SIGNATURE"] is False
    assert mod.REGISTER["CHAT_GROUP"] is False
    assert mod.REGISTER["NEED_SYSTEM_MESSAGES"] is False
    assert mod.REGISTER["NEWSLETTER"] is False
    # Иконка — data-uri PNG (без неё register падает ICON_REQUIRED).
    assert mod.REGISTER["ICON"]["DATA_IMAGE"].startswith("data:image/png;base64,")


def test_events_bindings():
    mod = _load()
    assert set(mod.EVENTS) == {
        "ONIMCONNECTORMESSAGEADD",
        "ONIMCONNECTORLINEDELETE",
        "ONIMCONNECTORSTATUSDELETE",
    }
    assert all(h.endswith("/webhook/b24/imconnector") for h in mod.EVENTS.values())
