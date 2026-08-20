"""Чистые функции хендлера активити БП: парсинг payload, [ссылки], SSRF-фильтр."""

import pytest

from app.web.routes.bizproc import (
    _activity_request,
    _first_str,
    _parse_document,
    _payload_dict,
    _redacted,
    _split_message,
    _url_is_public_https,
)


# ---------------------------------------------------------------------- #
# _payload_dict: JSON / form / мусор
# ---------------------------------------------------------------------- #
def test_payload_dict_json():
    assert _payload_dict(b'{"a": 1}', "application/json") == {"a": 1}


def test_payload_dict_json_without_content_type():
    """Очередь B24 может не ставить заголовок — тело начинается с '{'."""
    assert _payload_dict(b'{"a": 1}', "") == {"a": 1}


def test_payload_dict_form_flattens_php_keys():
    body = b"properties[message]=hi&auth[access_token]=tok&x=1"
    assert _payload_dict(body, "application/x-www-form-urlencoded") == {
        "properties": {"message": "hi"},
        "auth": {"access_token": "tok"},
        "x": "1",
    }


def test_payload_dict_form_indexed_arrays_become_lists():
    """Канонический PHP-массив B24 (http_build_query): a[0]/a[1]/… — список."""
    body = b"document_id[0]=crm&document_id[1]=CCrmDocumentDeal&document_id[2]=DEAL_123"
    assert _payload_dict(body, "application/x-www-form-urlencoded") == {
        "document_id": ["crm", "CCrmDocumentDeal", "DEAL_123"]
    }


def test_payload_dict_malformed_and_oversize():
    assert _payload_dict(b"not-json{", "application/json") is None
    assert _payload_dict(b"", "application/json") is None
    assert _payload_dict(b'{"a":1}' * 100000, "application/json") is None
    assert _payload_dict(b"binary\x00", "application/octet-stream") is None


# ---------------------------------------------------------------------- #
# Значения B24: обёртки списком, document_id
# ---------------------------------------------------------------------- #
def test_first_str():
    assert _first_str("x") == "x"
    assert _first_str(["x", "y"]) == "x"
    assert _first_str([1, "x"]) == "x"
    assert _first_str(None) is None
    assert _first_str([]) is None


def test_parse_document_variants():
    assert _parse_document(["crm", "CCrmDocumentDeal", "DEAL_12"]) == ("deal", 12)
    assert _parse_document(["crm", "CCrmDocumentLead", "LEAD_5"]) == ("lead", 5)
    assert _parse_document(["crm", "CCrmDocumentContact", "CONTACT_7"]) == ("contact", 7)
    assert _parse_document(["crm", "CCrmDocumentCompany", "COMPANY_9"]) == ("company", 9)
    assert _parse_document("deal_3") == ("deal", 3)
    assert _parse_document([]) is None
    assert _parse_document(["crm", "CCrmDocumentDeal", "SMART_INVOICE_1"]) is None
    assert _parse_document(123) is None


def _payload(**over):
    data = {
        "event_token": "evt-1",
        "document_id": ["crm", "CCrmDocumentDeal", "DEAL_123"],
        "properties": {"message": "Привет!"},
        "auth": {"access_token": "tok", "member_id": "m1", "user_id": "42"},
    }
    data.update(over)
    return data


def test_activity_request_full():
    ar = _activity_request(_payload())
    assert ar is not None
    assert (ar.entity_type, ar.entity_id) == ("deal", 123)
    assert ar.text == "Привет!"
    assert ar.user_id == 42
    assert ar.member_id == "m1"
    assert ar.access_token == "tok"
    assert ar.event_token == "evt-1"


def test_activity_request_message_wrapped_in_list():
    ar = _activity_request(_payload(properties={"message": ["Привет", "лишнее"]}))
    assert ar is not None and ar.text == "Привет"


def test_activity_request_rejects_missing_or_bad():
    assert _activity_request(_payload(properties={"message": ""})) is None
    assert _activity_request(_payload(document_id="UNKNOWN_1")) is None
    assert _activity_request({"properties": {"message": "x"}}) is None
    assert _activity_request(_payload(properties={"message": "x" * 4097})) is None


def test_activity_request_form_auth_flat():
    """form-вариант: auth плоский (access_token=…), message из php-ключа."""
    payload = _payload_dict(
        b"properties[message]=hi&document_id=LEAD_5&access_token=tok&member_id=m1&user_id=7",
        "application/x-www-form-urlencoded",
    )
    ar = _activity_request(payload)
    assert ar is not None
    assert (ar.entity_type, ar.entity_id) == ("lead", 5)
    assert ar.access_token == "tok" and ar.member_id == "m1" and ar.user_id == 7


# ---------------------------------------------------------------------- #
# [Ссылки] в тексте
# ---------------------------------------------------------------------- #
def test_split_message_without_links():
    assert _split_message("Просто текст") == (None, "Просто текст")


def test_split_message_link_and_caption():
    url, caption = _split_message("Договор:  [https://x.example/doc.pdf]  скачайте")
    assert url == "https://x.example/doc.pdf"
    assert caption == "Договор: скачайте"


def test_split_message_only_link_gives_empty_caption():
    assert _split_message("[https://x.example/a.png]") == ("https://x.example/a.png", "")


def test_split_message_keeps_remaining_links_in_text():
    """Вложением уходит только первая — паритет «один файл за шаг»."""
    url, caption = _split_message("[https://x.example/a.png] и [https://y.example/b.pdf]")
    assert url == "https://x.example/a.png"
    assert caption == "и [https://y.example/b.pdf]"


def test_split_message_ignores_http_links():
    """http — не кандидат на вложение (SSRF-фильтр их отверг бы): остаётся
    в тексте как есть; вложением идёт только первая https-ссылка."""
    url, caption = _split_message("Смотрите [http://x.example/a] и [https://y.example/b.png]")
    assert url == "https://y.example/b.png"
    assert caption == "Смотрите [http://x.example/a] и"


# ---------------------------------------------------------------------- #
# Редакция токенов для лога
# ---------------------------------------------------------------------- #
def test_redacted_strips_tokens():
    text = _redacted(_payload(auth={"access_token": "SECRET", "refresh_token": "R", "user_id": 1}))
    assert "SECRET" not in text
    assert "***" in text


def test_redacted_strips_root_level_tokens():
    """form-payload несёт токены плоско — маскируем и корневые ключи."""
    text = _redacted({"access_token": "SECRET", "x": 1})
    assert "SECRET" not in text
    assert '"x": 1' in text


# ---------------------------------------------------------------------- #
# SSRF-фильтр [ссылки]
# ---------------------------------------------------------------------- #
from app.web.routes import bizproc

_ADDR = {
    "public.example": "93.184.216.34",
    "private.example": "10.0.0.1",
    "localhost": "127.0.0.1",
    "meta.example": "169.254.169.254",
    "v6.example": "fd00::1",
    "mixed.example": "93.184.216.34",  # первый адрес публичный, второй — нет
}


@pytest.fixture
def dns(monkeypatch):
    async def fake(host: str) -> list[str]:
        addrs = [_ADDR[host]]
        if host == "mixed.example":
            addrs.append("10.0.0.2")
        return addrs

    monkeypatch.setattr(bizproc, "_resolve_host_addrs", fake)


@pytest.mark.asyncio
async def test_url_allows_public_https(dns):
    assert await _url_is_public_https("https://public.example/doc.pdf")


@pytest.mark.asyncio
async def test_url_rejects_non_https_and_ports_and_userinfo(dns):
    assert not await _url_is_public_https("http://public.example/doc.pdf")
    assert not await _url_is_public_https("https://public.example:8443/doc.pdf")
    assert not await _url_is_public_https("https://user:pw@public.example/doc.pdf")
    # Вне-диапазонный порт поднимает ValueError из urlparse — это 422, не 500.
    assert not await _url_is_public_https("https://public.example:70000/doc.pdf")
    assert not await _url_is_public_https("https://public.example:8O80/doc.pdf")


@pytest.mark.asyncio
async def test_url_rejects_private_targets(dns):
    assert not await _url_is_public_https("https://private.example/x")
    assert not await _url_is_public_https("https://localhost/x")
    assert not await _url_is_public_https("https://meta.example/x")  # 169.254.169.254
    assert not await _url_is_public_https("https://v6.example/x")  # fd00::/7
    assert not await _url_is_public_https("https://mixed.example/x")  # один адрес private


@pytest.mark.asyncio
async def test_url_rejects_unknown_host(monkeypatch):
    async def boom(host: str) -> list[str]:
        raise OSError("dns")

    monkeypatch.setattr(bizproc, "_resolve_host_addrs", boom)
    assert not await _url_is_public_https("https://nope.invalid/x")
