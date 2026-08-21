"""Подписи публичных медиа-ссылок: раунд-трип, просрочка, подмена."""


from app.media.public_url import sign_media_url, verify_media_sig

SECRET = "test-session-secret"


def _parts(url: str) -> tuple[int, int, str]:
    _, _, tail = url.rpartition("/media/public/")
    att, exp, sig = tail.split("/")
    return int(att), int(exp), sig


def test_sign_verify_roundtrip():
    url = sign_media_url("https://app.example", 42, secret=SECRET, ttl_sec=60)
    assert url.startswith("https://app.example/media/public/42/")
    att, exp, sig = _parts(url)
    assert att == 42
    assert verify_media_sig(att, exp, sig, secret=SECRET)


def test_expired_signature_fails():
    url = sign_media_url("https://app.example", 1, secret=SECRET, ttl_sec=-10)
    att, exp, sig = _parts(url)
    assert not verify_media_sig(att, exp, sig, secret=SECRET)


def test_wrong_secret_or_id_fails():
    url = sign_media_url("https://app.example", 7, secret=SECRET, ttl_sec=60)
    att, exp, sig = _parts(url)
    assert not verify_media_sig(att, exp, sig, secret="другой-секрет")
    assert not verify_media_sig(att + 1, exp, sig, secret=SECRET)


def test_sig_is_short_hex():
    url = sign_media_url("https://app.example", 1, secret=SECRET, ttl_sec=60)
    sig = _parts(url)[2]
    assert len(sig) == 32 and int(sig, 16) >= 0

