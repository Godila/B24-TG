from app.web.session import (
    SESSION_COOKIE,
    SESSION_TTL,
    create_session_payload,
    sign_session,
    verify_session,
)


def test_sign_and_verify_roundtrip():
    payload = create_session_payload(b24_user_id=15, deal_id=100)
    token = sign_session(payload, secret="test-secret")
    decoded = verify_session(token, secret="test-secret")
    assert decoded is not None
    assert decoded["b24_user_id"] == 15
    assert decoded["deal_id"] == 100


def test_verify_rejects_tampered_token():
    payload = create_session_payload(b24_user_id=15, deal_id=100)
    token = sign_session(payload, secret="test-secret")
    # Tamper: flip a character in the payload portion.
    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
    decoded = verify_session(tampered, secret="test-secret")
    assert decoded is None


def test_verify_rejects_wrong_secret():
    payload = create_session_payload(b24_user_id=15, deal_id=100)
    token = sign_session(payload, secret="secret-a")
    decoded = verify_session(token, secret="secret-b")
    assert decoded is None


def test_verify_rejects_expired_token():
    # Payload already expired (exp in the past).
    payload = create_session_payload(b24_user_id=15, deal_id=100, ttl=-10)
    token = sign_session(payload, secret="test-secret")
    decoded = verify_session(token, secret="test-secret")
    assert decoded is None


def test_session_cookie_name():
    assert SESSION_COOKIE == "btg_sess"
    assert SESSION_TTL == 8 * 3600
