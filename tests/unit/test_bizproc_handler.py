"""Ядро хендлера активити БП (без HTTP): авторизация, резолв, постановка.

Сетевые зависимости (_token_alive, скачивание) мокаются — тесты герметичные.
"""

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import (
    Attachment,
    B24Token,
    Base,
    Contact,
    Dialog,
    Message,
    MessageDirection,
    Messenger,
    OutboxItem,
    TgAccount,
    TgAccountStatus,
)
from app.web.routes import bizproc

WEBHOOK_SECRET = "test-webhook-secret-123"


def _payload(**over) -> bytes:
    data = {
        "event_token": "evt-1",
        "document_id": ["crm", "CCrmDocumentDeal", "DEAL_123"],
        "properties": {"message": "Привет из бизнес-процесса!"},
        "auth": {"access_token": "tok", "member_id": "m1", "user_id": "42"},
    }
    data.update(over)
    return json.dumps(data).encode()


@pytest.fixture(autouse=True)
def _clean_caches():
    bizproc._selfcheck_ok.clear()
    bizproc._seen_events.clear()
    yield
    bizproc._selfcheck_ok.clear()
    bizproc._seen_events.clear()


@pytest.fixture
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        s.add_all(
            [
                B24Token(
                    member_id="m1",
                    access_token="tok",
                    refresh_token="r",
                    client_endpoint="https://p/rest/",
                    portal="https://p",
                    user_id=1,
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                ),
                Contact(id=50, messenger=Messenger.tg, external_user_id="u50", crm_contact_id=500),
                TgAccount(
                    id=7, messenger=Messenger.tg, phone="79001112233", status=TgAccountStatus.active
                ),
                # Два диалога одной сделки: «последний» — свежий по last_msg_at.
                Message(
                    id=1,
                    dialog_id=11,
                    direction=MessageDirection.inbound,
                    text="было входящее",
                ),
                Dialog(
                    id=10,
                    contact_id=50,
                    messenger=Messenger.tg,
                    external_chat_id="c900",
                    account_id=7,
                    crm_deal_id=123,
                    crm_entity_type="deal",
                    last_msg_at=datetime(2026, 1, 1, tzinfo=UTC),
                ),
                Dialog(
                    id=11,
                    contact_id=50,
                    messenger=Messenger.tg,
                    external_chat_id="c901",
                    account_id=7,
                    crm_deal_id=123,
                    crm_entity_type="deal",
                    last_msg_at=datetime(2026, 2, 1, tzinfo=UTC),
                ),
                # Сделочной эры строка без типа + ни одного сообщения:
                # legacy-NULL резолвится, is_initiation=True.
                Dialog(
                    id=13,
                    contact_id=50,
                    messenger=Messenger.tg,
                    external_chat_id="c903",
                    account_id=7,
                    crm_deal_id=555,
                ),
            ]
        )
        await s.commit()
    yield factory
    await engine.dispose()


def _secret_auth(monkeypatch):
    monkeypatch.setenv("B24_WEBHOOK_SECRET", WEBHOOK_SECRET)
    from app.config import get_settings

    get_settings.cache_clear()


async def _call(db, body: bytes, secret: str | None = None):
    async with db() as s:
        return await bizproc.handle_bizproc_message(body, "application/json", secret, s)


async def _count(db, model) -> int:
    async with db() as s:
        return (await s.execute(select(func.count()).select_from(model))).scalar_one()


async def _outbound_count(db) -> int:
    """Только исходящие: сид содержит входящее сообщение (для is_initiation)."""
    async with db() as s:
        return (
            await s.execute(
                select(func.count())
                .select_from(Message)
                .where(Message.direction == MessageDirection.outbound)
            )
        ).scalar_one()


# ---------------------------------------------------------------------- #
# Авторизация
# ---------------------------------------------------------------------- #
async def test_unauthorized_member_mismatch_401(db, monkeypatch):
    alive = AsyncMock(return_value=False)
    monkeypatch.setattr(bizproc, "_token_alive", alive)
    resp = await _call(
        db, _payload(auth={"access_token": "tok", "member_id": "OTHER", "user_id": "1"})
    )
    assert resp.status_code == 401
    alive.assert_not_awaited()  # member_id-сверка отсекла до сети
    assert await _outbound_count(db) == 0


async def test_member_ok_selfcheck_fail_401(db, monkeypatch):
    monkeypatch.setattr(bizproc, "_token_alive", AsyncMock(return_value=False))
    resp = await _call(db, _payload())
    assert resp.status_code == 401


async def test_secret_header_skips_selfcheck(db, monkeypatch):
    _secret_auth(monkeypatch)
    alive = AsyncMock(return_value=False)
    monkeypatch.setattr(bizproc, "_token_alive", alive)
    resp = await _call(db, _payload(), secret=WEBHOOK_SECRET)
    assert resp.status_code == 200
    alive.assert_not_awaited()


async def test_selfcheck_positive_cache_one_network_call(db, monkeypatch):
    alive = AsyncMock(return_value=True)
    monkeypatch.setattr(bizproc, "_token_alive", alive)
    for _ in range(2):
        resp = await _call(db, _payload())
        assert resp.status_code == 200
    alive.assert_awaited_once()  # бёрст БП = 1 user.current на токен


# ---------------------------------------------------------------------- #
# Резолв «последнего диалога»
# ---------------------------------------------------------------------- #
async def test_text_message_enqueued_to_latest_dialog(db, monkeypatch):
    _secret_auth(monkeypatch)
    resp = await _call(db, _payload(), secret=WEBHOOK_SECRET)
    assert resp.status_code == 200
    assert json.loads(resp.body) == {"status": "queued"}

    async with db() as s:
        items = (await s.execute(select(OutboxItem))).scalars().all()
        assert len(items) == 1
        # Свежий диалог (c901, last_msg_at=2026-02-01), автор — юзер БП.
        assert items[0].external_chat_id == "c901"
        assert items[0].text == "Привет из бизнес-процесса!"
        assert items[0].attachment_id is None
        assert items[0].is_initiation is False  # в диалоге есть входящее
        msg = await s.get(Message, items[0].message_id)
        assert msg.author_user_id == 42
        assert msg.direction == MessageDirection.outbound


async def test_legacy_null_deal_type_and_initiation(db, monkeypatch):
    """Строка без crm_entity_type (сделочной эры) резолвится; диалог без
    входящих = инициация (throttle-ветка outbox)."""
    _secret_auth(monkeypatch)
    resp = await _call(db, _payload(document_id="DEAL_555"), secret=WEBHOOK_SECRET)
    assert resp.status_code == 200
    async with db() as s:
        items = (await s.execute(select(OutboxItem))).scalars().all()
        assert items[0].external_chat_id == "c903"
        assert items[0].is_initiation is True


async def test_no_dialog_409_nothing_enqueued(db, monkeypatch):
    _secret_auth(monkeypatch)
    resp = await _call(db, _payload(document_id="DEAL_999"), secret=WEBHOOK_SECRET)
    assert resp.status_code == 409
    assert await _outbound_count(db) == 0
    assert await _count(db, OutboxItem) == 0


async def test_company_unsupported_409(db, monkeypatch):
    _secret_auth(monkeypatch)
    resp = await _call(db, _payload(document_id="COMPANY_1"), secret=WEBHOOK_SECRET)
    assert resp.status_code == 409


async def test_lead_and_deal_id_spaces_independent(db, monkeypatch):
    _secret_auth(monkeypatch)
    async with db() as s:
        s.add(
            Dialog(
                id=12,
                contact_id=50,
                messenger=Messenger.tg,
                external_chat_id="c902",
                account_id=7,
                crm_deal_id=77,
                crm_entity_type="lead",
            )
        )
        await s.commit()
    # Лид находит свой диалог, DEAL_77 — нет (разные id-пространства).
    resp = await _call(db, _payload(document_id="LEAD_77"), secret=WEBHOOK_SECRET)
    assert resp.status_code == 200
    resp = await _call(
        db, _payload(document_id="DEAL_77", event_token="e2"), secret=WEBHOOK_SECRET
    )
    assert resp.status_code == 409


async def test_contact_resolution_via_crm_contact_id(db, monkeypatch):
    _secret_auth(monkeypatch)
    resp = await _call(db, _payload(document_id="CONTACT_500"), secret=WEBHOOK_SECRET)
    assert resp.status_code == 200
    async with db() as s:
        items = (await s.execute(select(OutboxItem))).scalars().all()
        assert items[0].external_chat_id == "c901"  # свежий диалог контакта


async def test_no_account_409(db, monkeypatch):
    _secret_auth(monkeypatch)
    async with db() as s:
        dlg = await s.get(Dialog, 11)
        dlg.account_id = None
        await s.commit()
    resp = await _call(db, _payload(), secret=WEBHOOK_SECRET)
    assert resp.status_code == 409


async def test_banned_account_409(db, monkeypatch):
    _secret_auth(monkeypatch)
    async with db() as s:
        acc = await s.get(TgAccount, 7)
        acc.status = TgAccountStatus.banned
        await s.commit()
    resp = await _call(db, _payload(), secret=WEBHOOK_SECRET)
    assert resp.status_code == 409


# ---------------------------------------------------------------------- #
# Канонический form-payload B24 (индексные PHP-массивы)
# ---------------------------------------------------------------------- #
async def test_form_payload_indexed_arrays(db, monkeypatch):
    _secret_auth(monkeypatch)
    body = (
        "properties[message]=Привет"
        "&document_id[0]=crm&document_id[1]=CCrmDocumentDeal&document_id[2]=DEAL_123"
        "&event_token=evt-form"
    ).encode()
    async with db() as s:
        resp = await bizproc.handle_bizproc_message(
            body, "application/x-www-form-urlencoded", WEBHOOK_SECRET, s
        )
    assert resp.status_code == 200
    assert await _count(db, OutboxItem) == 1


# ---------------------------------------------------------------------- #
# Файл по [ссылке]
# ---------------------------------------------------------------------- #
@pytest.fixture
def mock_download(monkeypatch, tmp_path):
    """Фабрика подмен сети: _url_is_public_https → True, стрим — MockTransport."""
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path / "media"))
    from app.config import get_settings
    from app.media.storage import get_media_storage

    get_settings.cache_clear()
    get_media_storage.cache_clear()
    real_client = httpx.AsyncClient

    def install(content: bytes, mime: str, status: int = 200) -> None:
        async def fake_public(url: str) -> bool:
            return True

        monkeypatch.setattr(bizproc, "_url_is_public_https", fake_public)

        def factory(**kwargs):
            kwargs.pop("transport", None)
            return real_client(
                transport=httpx.MockTransport(
                    lambda req: httpx.Response(
                        status, content=content, headers={"Content-Type": mime}
                    )
                ),
                **kwargs,
            )

        monkeypatch.setattr(bizproc.httpx, "AsyncClient", factory)

    return install


async def test_link_downloads_and_attaches(db, monkeypatch, mock_download):
    _secret_auth(monkeypatch)
    mock_download(b"\x89PNG fake", "image/png")
    body = _payload(properties={"message": "Договор [https://x.example/doc.png] скачайте"})
    resp = await _call(db, body, secret=WEBHOOK_SECRET)
    assert resp.status_code == 200
    async with db() as s:
        items = (await s.execute(select(OutboxItem))).scalars().all()
        assert len(items) == 1 and items[0].attachment_id is not None
        # В очередь — реальный caption; в Message.text — тоже он (не пуст).
        assert items[0].text == "Договор скачайте"
        att = await s.get(Attachment, items[0].attachment_id)
        assert att.mime_type == "image/png"
        assert att.file_name == "doc.png"
        msg = await s.get(Message, items[0].message_id)
        assert msg.text == "Договор скачайте"


async def test_link_only_message_gets_placeholder(db, monkeypatch, mock_download):
    _secret_auth(monkeypatch)
    mock_download(b"\x89PNG fake", "image/png")
    body = _payload(properties={"message": "[https://x.example/doc.png]"})
    resp = await _call(db, body, secret=WEBHOOK_SECRET)
    assert resp.status_code == 200
    async with db() as s:
        items = (await s.execute(select(OutboxItem))).scalars().all()
        assert items[0].text == ""  # пустой caption — как у upload-роута
        msg = await s.get(Message, items[0].message_id)
        assert msg.text  # плейсхолдер («[фото]»/«[файл]» по типу)


async def test_http_link_stays_in_text_no_attachment(db, monkeypatch):
    """http-[ссылка] — не кандидат на вложение: текст уходит как есть."""
    _secret_auth(monkeypatch)
    body = _payload(properties={"message": "Смотрите [http://x.example/a] позже"})
    resp = await _call(db, body, secret=WEBHOOK_SECRET)
    assert resp.status_code == 200
    async with db() as s:
        items = (await s.execute(select(OutboxItem))).scalars().all()
        assert items[0].attachment_id is None
        assert items[0].text == "Смотрите [http://x.example/a] позже"


async def test_caption_too_long_422_before_download(db, monkeypatch, mock_download):
    """Подпись >1024 уронит отправку в outbox уже после зелёного шага —
    честный не-200 до скачивания."""
    _secret_auth(monkeypatch)
    mock_download(b"\x89PNG fake", "image/png")
    body = _payload(
        properties={"message": "[https://x.example/doc.png] " + "x" * 1100}
    )
    resp = await _call(db, body, secret=WEBHOOK_SECRET)
    assert resp.status_code == 422
    assert await _count(db, OutboxItem) == 0


async def test_download_bad_mime_415(db, monkeypatch, mock_download):
    _secret_auth(monkeypatch)
    mock_download(b"<svg/>", "image/svg+xml")  # REJECTED_MIME
    body = _payload(properties={"message": "[https://x.example/e.svg]"})
    with pytest.raises(HTTPException) as ei:
        await _call(db, body, secret=WEBHOOK_SECRET)
    assert ei.value.status_code == 415
    assert await _count(db, OutboxItem) == 0


async def test_download_http_error_502(db, monkeypatch, mock_download):
    _secret_auth(monkeypatch)
    mock_download(b"gone", "image/png", status=404)
    body = _payload(properties={"message": "[https://x.example/404.png]"})
    with pytest.raises(HTTPException) as ei:
        await _call(db, body, secret=WEBHOOK_SECRET)
    assert ei.value.status_code == 502


async def test_download_too_large_413_atomic(db, monkeypatch, mock_download, tmp_path):
    _secret_auth(monkeypatch)
    from app.media.storage import get_media_storage

    limit = get_media_storage().max_size_bytes
    mock_download(b"x" * (limit + 1), "image/png")
    body = _payload(properties={"message": "[https://x.example/doc.png]"})
    with pytest.raises(HTTPException) as ei:
        await _call(db, body, secret=WEBHOOK_SECRET)
    assert ei.value.status_code == 413
    # All-or-nothing: ни строк, ни файла.
    assert await _outbound_count(db) == 0
    assert await _count(db, OutboxItem) == 0
    assert await _count(db, Attachment) == 0
    out_dir = tmp_path / "media" / "out"
    assert not out_dir.exists() or not any(out_dir.glob("*"))


async def test_private_url_rejected_422(db, monkeypatch):
    _secret_auth(monkeypatch)
    monkeypatch.setattr(
        bizproc, "_url_is_public_https", AsyncMock(return_value=False)
    )
    body = _payload(properties={"message": "[https://internal/file.pdf]"})
    with pytest.raises(HTTPException) as ei:
        await _call(db, body, secret=WEBHOOK_SECRET)
    assert ei.value.status_code == 422
    assert await _count(db, OutboxItem) == 0


# ---------------------------------------------------------------------- #
# Идемпотентность event_token
# ---------------------------------------------------------------------- #
async def test_duplicate_event_token_no_second_send(db, monkeypatch):
    _secret_auth(monkeypatch)
    resp1 = await _call(db, _payload(), secret=WEBHOOK_SECRET)
    resp2 = await _call(db, _payload(), secret=WEBHOOK_SECRET)
    assert resp1.status_code == 200
    assert json.loads(resp2.body) == {"status": "duplicate"}
    assert await _count(db, OutboxItem) == 1
