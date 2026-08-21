"""Вебхук ONIMCONNECTOR*: авторизация, дедуб, постановка, LINEDELETE."""

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.b24.openlines import OperatorEvent, OperatorMessage
from app.models import (
    B24Token,
    Base,
    Contact,
    Dialog,
    Message,
    MessageDirection,
    MessageStatus,
    Messenger,
    OutboxItem,
    OutboxStatus,
    TgAccount,
    TgAccountStatus,
)
from app.web.routes import bizproc, openline

WEBHOOK_SECRET = "test-webhook-secret-123"


@pytest.fixture(autouse=True)
def _clean_caches():
    """Общий с bizproc кэш self-check не должен течь между тестами."""
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
                    scope="im,imopenlines",
                    application_token="apptok-123",
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                ),
                Contact(id=50, messenger=Messenger.tg, external_user_id="u50"),
                TgAccount(
                    id=7,
                    messenger=Messenger.tg,
                    phone="79001112233",
                    status=TgAccountStatus.active,
                    ol_line_id="107",
                    ol_active=True,
                ),
                Dialog(
                    id=11,
                    contact_id=50,
                    messenger=Messenger.tg,
                    external_chat_id="c900",
                    account_id=7,
                ),
            ]
        )
        await s.commit()
    yield factory
    await engine.dispose()


def _ev(message_id=86497, chat_id="11", text="Здравствуйте!") -> OperatorEvent:
    return OperatorEvent(
        connector="chatmost",
        line="107",
        messages=[
            OperatorMessage(
                im_chat_id=1807,
                im_message_id=message_id,
                chat_id=chat_id,
                text=text,
                user_id=27,
            )
        ],
    )


# ---------------------------------------------------------------------- #
# Авторизация (эшелоны)
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_authorized_by_secret_header(db, monkeypatch):
    monkeypatch.setenv("B24_WEBHOOK_SECRET", WEBHOOK_SECRET)
    from app.config import get_settings

    get_settings.cache_clear()
    try:
        async with db() as s:
            assert await openline._authorized(
                WEBHOOK_SECRET, {"event": "ONIMCONNECTORMESSAGEADD"}, s
            )
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_authorized_by_application_token(db):
    async with db() as s:
        payload = {"event": "x", "auth": {"member_id": "m1", "application_token": "apptok-123"}}
        assert await openline._authorized(None, payload, s)


@pytest.mark.asyncio
async def test_rejected_wrong_application_token_and_member(db):
    async with db() as s:
        assert not await openline._authorized(
            None, {"auth": {"member_id": "m1", "application_token": "чужой"}}, s
        )
        # Валидный application_token — сам по себе доказательство (секрет
        # приложения), даже при расхождении member_id.
        assert await openline._authorized(
            None, {"auth": {"member_id": "чужой", "application_token": "apptok-123"}}, s
        )


@pytest.mark.asyncio
async def test_member_id_with_live_token_fallback(db, monkeypatch):
    async with db() as s:
        # self-check живёт в bizproc (общий хелпер вебхуков)
        monkeypatch.setattr(bizproc, "_token_alive", AsyncMock(return_value=True))
        payload = {"auth": {"member_id": "m1", "access_token": "tok"}}
        assert await openline._authorized(None, payload, s)


@pytest.mark.asyncio
async def test_member_id_without_token_rejected(db):
    """До переустановки (application_token NULL) и без access_token — 401."""
    async with db() as s:
        assert not await openline._authorized(None, {"auth": {"member_id": "m1"}}, s)


# ---------------------------------------------------------------------- #
# Операторские сообщения
# ---------------------------------------------------------------------- #


async def _outbox(session: AsyncSession) -> list[OutboxItem]:
    return list(
        (await session.execute(select(OutboxItem).order_by(OutboxItem.id))).scalars().all()
    )


@pytest.mark.asyncio
async def test_operator_message_queued_to_outbox(db):
    async with db() as s:
        resp = await openline.handle_operator_event(_ev(), s)
        assert resp.status_code == 200
        assert json.loads(resp.body) == {"status": "queued", "queued": 1, "skipped": 0}

    async with db() as s:
        msg = (
            await s.execute(select(Message).order_by(Message.id))
        ).scalars().first()
        assert msg.direction == MessageDirection.outbound
        assert msg.text == "Здравствуйте!"
        assert msg.status == MessageStatus.pending
        assert msg.author_user_id == 27
        assert msg.b24_im_chat_id == 1807
        assert msg.b24_im_message_id == 86497
        (item,) = await _outbox(s)
        assert item.tg_account_id == 7
        assert item.external_chat_id == "c900"
        assert item.status == OutboxStatus.queued
        assert item.message_id == msg.id
        assert item.is_initiation is False


@pytest.mark.asyncio
async def test_duplicate_event_not_requeued(db):
    async with db() as s:
        first = await openline.handle_operator_event(_ev(), s)
        assert json.loads(first.body)["queued"] == 1
        second = await openline.handle_operator_event(_ev(), s)
        assert json.loads(second.body) == {"status": "queued", "queued": 0, "skipped": 1}
    async with db() as s:
        assert len(await _outbox(s)) == 1


@pytest.mark.asyncio
async def test_dialog_of_other_line_skipped(db):
    """chat.id валидный, но диалог принадлежит другому аккаунту — skip."""
    async with db() as s:
        s.add(
            Dialog(
                id=12, contact_id=50, messenger=Messenger.tg, external_chat_id="c901", account_id=8
            )
        )
        await s.commit()
        resp = await openline.handle_operator_event(_ev(chat_id="12"), s)
        assert json.loads(resp.body)["skipped"] == 1
    async with db() as s:
        assert await _outbox(s) == []


@pytest.mark.asyncio
async def test_no_binding_line_ignored(db):
    async with db() as s:
        resp = await openline.handle_operator_event(
            OperatorEvent(connector="chatmost", line="999", messages=_ev().messages), s
        )
        assert json.loads(resp.body) == {"status": "no_binding"}


@pytest.mark.asyncio
async def test_empty_text_skipped(db):
    async with db() as s:
        resp = await openline.handle_operator_event(_ev(text=""), s)
        assert json.loads(resp.body)["skipped"] == 1
    async with db() as s:
        assert await _outbox(s) == []


# ---------------------------------------------------------------------- #
# LINEDELETE / STATUSDELETE
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_line_delete_unbinds_account(db):
    payload = {"event": "ONIMCONNECTORLINEDELETE", "data": {"LINE": 107}}
    async with db() as s:
        await openline._handle_line_delete(s, payload, full=True)
    async with db() as s:
        acc = await s.get(TgAccount, 7)
        assert acc.ol_line_id is None and acc.ol_active is False


@pytest.mark.asyncio
async def test_status_delete_deactivates_keeps_binding(db):
    payload = {
        "event": "ONIMCONNECTORSTATUSDELETE",
        "data": {"CONNECTOR": "chatmost", "LINE": "107"},
    }
    async with db() as s:
        await openline._handle_line_delete(s, payload, full=False)
    async with db() as s:
        acc = await s.get(TgAccount, 7)
        assert acc.ol_line_id == "107" and acc.ol_active is False


# ---------------------------------------------------------------------- #
# HTTP-роут (TestClient): form-php тело + полный путь
# ---------------------------------------------------------------------- #


@pytest.fixture
def client(db, monkeypatch):
    monkeypatch.setattr(openline, "async_session", db)
    monkeypatch.setattr(bizproc, "_token_alive", AsyncMock(return_value=False))
    from app.web.app import create_app

    return TestClient(create_app())


_FORM_BODY = (
    "event=ONIMCONNECTORMESSAGEADD"
    "&data[CONNECTOR]=chatmost"
    "&data[LINE]=107"
    "&data[MESSAGES][0][im][chat_id]=1807"
    "&data[MESSAGES][0][im][message_id]=86497"
    "&data[MESSAGES][0][message][user_id]=27"
    "&data[MESSAGES][0][message][text]=Из+чата+линии"
    "&data[MESSAGES][0][chat][id]=11"
    "&auth[member_id]=m1"
    "&auth[application_token]=apptok-123"
)


def test_route_form_payload_queues_message(client, db):
    resp = client.post(
        "/webhook/b24/imconnector",
        content=_FORM_BODY,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "queued", "queued": 1, "skipped": 0}


def test_route_unauthorized_401(client):
    resp = client.post(
        "/webhook/b24/imconnector",
        content=_FORM_BODY.replace("apptok-123", "чужой"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert resp.status_code == 401


def test_route_malformed_body_422(client):
    resp = client.post(
        "/webhook/b24/imconnector",
        content=b"{not json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 422


def test_route_line_delete_form(client, db):
    resp = client.post(
        "/webhook/b24/imconnector",
        content="event=ONIMCONNECTORLINEDELETE&data[LINE]=107&auth[member_id]=m1&auth[application_token]=apptok-123",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert resp.status_code == 200


# ---------------------------------------------------------------------- #
# Фиксы ревью
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_status_delete_foreign_connector_ignored(db):
    """STATUSDELETE чужого коннектора на той же линии не глушит привязку."""
    payload = {
        "event": "ONIMCONNECTORSTATUSDELETE",
        "data": {"CONNECTOR": "someone_else", "LINE": "107"},
    }
    async with db() as s:
        await openline._handle_line_delete(s, payload, full=False)
    async with db() as s:
        acc = await s.get(TgAccount, 7)
        assert acc.ol_line_id == "107" and acc.ol_active is True


@pytest.mark.asyncio
async def test_unique_index_blocks_duplicate_im_message(db):
    """Гонка двух доставок события: unique (dialog_id, b24_im_message_id)."""
    from sqlalchemy.exc import IntegrityError

    async with db() as s:
        s.add(
            Message(
                dialog_id=11,
                direction=MessageDirection.outbound,
                b24_im_chat_id=1807,
                b24_im_message_id=86497,
            )
        )
        await s.commit()
        s.add(
            Message(
                dialog_id=11,
                direction=MessageDirection.outbound,
                b24_im_chat_id=1807,
                b24_im_message_id=86497,
            )
        )
        with pytest.raises(IntegrityError):
            await s.commit()


@pytest.mark.asyncio
async def test_operator_message_bumps_dialog_last_msg_at(db):
    async with db() as s:
        await openline.handle_operator_event(_ev(), s)
    async with db() as s:
        dialog = await s.get(Dialog, 11)
        assert dialog.last_msg_at is not None
