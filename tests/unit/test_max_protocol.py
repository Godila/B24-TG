"""protocol.py MAX: классификация cmd=3, извлечение токена, userAgent."""

from app.messaging.max.protocol import (
    MaxAuthError,
    MaxProtocolError,
    MaxQrDisabledError,
    MaxQrExpiredError,
    MaxThrottleError,
    build_user_agent,
    classify_error,
    extract_token,
)


def test_classify_track_not_found():
    assert isinstance(classify_error(289, {"error": "track.not.found"}), MaxQrExpiredError)


def test_classify_qr_disabled_is_auth_error():
    exc = classify_error(288, {"error": "qr_login.disabled"})
    assert isinstance(exc, MaxQrDisabledError)
    assert isinstance(exc, MaxAuthError)  # субкласс: провайдер должен умереть


def test_classify_nested_code_dict():
    exc = classify_error(19, {"error": {"code": "SESSION_EXPIRED"}})
    assert isinstance(exc, MaxAuthError)


def test_classify_chat_level_error_is_not_auth():
    """chat.forbidden на MSG_SEND — ошибка чата, НЕ смерть провайдера."""
    exc = classify_error(64, {"error": "chat.forbidden"})
    assert not isinstance(exc, MaxAuthError)
    assert isinstance(exc, MaxProtocolError)


def test_classify_throttle():
    exc = classify_error(64, {"error": "too.many.messages"})
    assert isinstance(exc, MaxThrottleError)
    assert exc.retry_after_seconds == 30


def test_classify_unknown_is_protocol_error():
    exc = classify_error(64, {"error": "something.odd"})
    assert isinstance(exc, MaxProtocolError)
    assert exc.opcode == 64


def test_extract_token_direct_path():
    payload = {"tokenAttrs": {"LOGIN": {"token": "An_abc"}}, "profile": {}}
    assert extract_token(payload) == "An_abc"


def test_extract_token_recursive_fallback_prefers_login():
    payload = {"deep": {"refresh_token": "SHORT", "session": {"loginToken": "An_long_one"}}}
    assert extract_token(payload) == "An_long_one"


def test_extract_token_none_when_absent():
    assert extract_token({"profile": {"id": 1}}) is None


def test_build_user_agent_substitutes_version():
    ua = build_user_agent("27.0.1", "UA-Test")
    assert ua["appVersion"] == "27.0.1"
    assert ua["headerUserAgent"] == "UA-Test"
    assert ua["isPwa"] is False
