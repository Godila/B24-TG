"""Схемы сериализуют naive-datetime (SQLite-дев) как aware UTC (UX-05).

Без зоны браузер трактует ISO-строку как локальное время — сдвиг на
таймзону клиента. Прод (PostgreSQL timestamptz) отдаёт aware-значения;
валидатор делает дев идентичным прод.
"""

from datetime import UTC, datetime

from app.web.schemas import DialogOut, InboxDialogOut, MessageOut

#: pydantic сериализует UTC как суффикс «Z», допускаем и «+00:00».
_UTC_SUFFIXES = ("Z", "+00:00")


def _dumped(model, field: str):
    return model.model_dump(mode="json")[field]


def test_naive_datetime_becomes_utc():
    naive = datetime(2026, 8, 16, 22, 44, 41, 901432)  # noqa: DTZ001 - тестируем именно naive
    msg = _dumped(
        MessageOut(id=1, dialog_id=2, direction="in", status="sent", created_at=naive),
        "created_at",
    )
    assert msg.endswith(_UTC_SUFFIXES)
    dlg = _dumped(
        DialogOut(id=1, contact_id=2, messenger="tg", external_chat_id="x", last_msg_at=naive),
        "last_msg_at",
    )
    assert dlg.endswith(_UTC_SUFFIXES)
    inbox = _dumped(
        InboxDialogOut(id=1, contact_id=2, messenger="tg", is_mine=True, last_msg_at=naive),
        "last_msg_at",
    )
    assert inbox.endswith(_UTC_SUFFIXES)


def test_aware_datetime_passes_through_unchanged():
    aware = datetime(2026, 8, 16, 22, 44, 41, tzinfo=UTC)
    out = _dumped(
        MessageOut(id=1, dialog_id=2, direction="in", status="sent", created_at=aware),
        "created_at",
    )
    assert out.endswith(_UTC_SUFFIXES)
    assert "22:44:41" in out


def test_none_stays_none():
    assert (
        _dumped(
            DialogOut(id=1, contact_id=2, messenger="tg", external_chat_id="x"),
            "last_msg_at",
        )
        is None
    )
