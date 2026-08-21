"""Чистые функции imconnector-коннектора: BB-чистка, парсер события, билдеры."""

from datetime import UTC, datetime

import pytest

from app.b24.openlines import (
    CONNECTOR_ID,
    build_delivery_message,
    build_send_message,
    parse_operator_event,
    strip_bb,
    to_unixsec,
)


def test_strip_bb_removes_known_tags_only():
    assert strip_bb("[b]Имя:[/b] [br]Добрый день!") == "Имя: Добрый день!"
    assert strip_bb("[url=https://x.ru]ссылка[/url]") == "ссылка"
    assert strip_bb("[i]курсив[/i] [u]подчёрк[/u] [s]зачёрк[/s]") == "курсив подчёрк зачёрк"
    # Не-BB скобки — текст клиента, не трогаем (fail-closed к тексту).
    assert strip_bb("[не тег] [123]") == "[не тег] [123]"


def test_to_unixsec_naive_is_utc():
    naive = datetime(2026, 8, 20, 12, 0, 0)  # noqa: DTZ001 — проверяем naive-приведение
    assert to_unixsec(naive) == to_unixsec(
        datetime(2026, 8, 20, 12, 0, 0, tzinfo=UTC)
    )


def _event_payload(messages, connector=CONNECTOR_ID, line=107) -> dict:
    return {
        "event": "ONIMCONNECTORMESSAGEADD",
        "data": {"CONNECTOR": connector, "LINE": line, "MESSAGES": messages},
        "auth": {"member_id": "m1"},
    }


def test_parse_operator_event_json():
    ev = parse_operator_event(
        _event_payload(
            [
                {
                    "im": {"chat_id": 1807, "message_id": 86497},
                    "message": {"user_id": 27, "text": "[b]Света:[/b] [br]Привет"},
                    "chat": {"id": "11"},
                }
            ]
        )
    )
    assert ev is not None
    assert ev.line == "107"
    assert ev.connector == CONNECTOR_ID
    assert len(ev.messages) == 1
    m = ev.messages[0]
    assert (m.im_chat_id, m.im_message_id, m.chat_id, m.text, m.user_id) == (
        1807,
        86497,
        "11",
        "Света: Привет",
        27,
    )


def test_parse_operator_event_rejects_dict_messages():
    """MESSAGES-объект вместо массива — fail-closed None: form-парсер уже
    listифицирует числовые уровни, JSON даёт array — dict быть не должно."""
    ev = parse_operator_event(
        _event_payload(
            {
                "0": {
                    "im": {"chat_id": "1807", "message_id": "86497"},
                    "message": {"user_id": "27", "text": "Привет"},
                    "chat": {"id": "11"},
                }
            }
        )
    )
    assert ev is None


def test_parse_operator_event_skips_broken_items():
    ev = parse_operator_event(
        _event_payload(
            [
                {"im": {"chat_id": "x", "message_id": 1}, "chat": {"id": "11"}},  # не-int
                {"message": {"text": "нет im"}},  # нет im/chat
                {"im": {"chat_id": 1, "message_id": 2}, "chat": {}},  # нет chat.id
                "мусор",
                {
                    "im": {"chat_id": 5, "message_id": 6},
                    "chat": {"id": "11"},
                    "message": {"user_id": 3, "text": "ок"},
                },
            ]
        )
    )
    assert ev is not None
    assert len(ev.messages) == 1
    assert ev.messages[0].im_message_id == 6


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"data": "мусор"},
        {"data": {"CONNECTOR": CONNECTOR_ID}},  # нет LINE/MESSAGES
        {"data": {"CONNECTOR": CONNECTOR_ID, "LINE": 1, "MESSAGES": {}}},
        {"data": {"CONNECTOR": CONNECTOR_ID, "LINE": 1, "MESSAGES": [{"im": {}}]}},
    ],
)
def test_parse_operator_event_garbage_returns_none(payload):
    assert parse_operator_event(payload) is None


def test_build_send_message_shape():
    msg = build_send_message(
        message_id="991",
        dialog_id=11,
        date_unixsec=1773265993,
        text="Привет",
        user_id="tg_u50",
        user_name="Иван",
        user_last_name="Иванов",
        files=[{"url": "https://x/f.png", "name": "f.png"}],
        chat_name="Чат с Иваном",
    )
    assert msg == {
        "user": {"id": "tg_u50", "name": "Иван", "last_name": "Иванов"},
        "message": {
            "id": "991",
            "date": 1773265993,
            "text": "Привет",
            "files": [{"url": "https://x/f.png", "name": "f.png"}],
        },
        "chat": {"id": "11", "name": "Чат с Иваном"},
    }


def test_build_send_message_minimal():
    msg = build_send_message(
        message_id="1", dialog_id=2, date_unixsec=3, text=None, user_id="x", user_name=None
    )
    assert msg == {
        "user": {"id": "x"},
        "message": {"id": "1", "date": 3, "text": ""},
        "chat": {"id": "2"},
    }


def test_build_delivery_message_shape():
    assert build_delivery_message(
        dialog_id=11, im_chat_id=1807, im_message_id=86497, date_unixsec=1773265993
    ) == {
        "im": {"chat_id": 1807, "message_id": 86497},
        "message": {"id": "86497", "date": 1773265993},
        "chat": {"id": "11"},
    }
