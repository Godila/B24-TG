"""Feed-уведомления (Wazzup-паритет): сборщики, воркер, репозиторий, роут.

Воркер гоняется на mock-repo + mock-im (по образцу test_crm_sync_worker);
репозиторий — in-memory SQLite (по образцу test_crm_sync_repo); роут —
TestClient с подменённой сессией (по образцу test_public_media_route).
"""

import time
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.b24.client import Bitrix24Error
from app.b24.notify import (
    build_keyboard,
    build_notification_text,
    crm_card_url,
    sign_dismiss_url,
    verify_dismiss_sig,
)
from app.bridge.crm_sync_repo import SqlAlchemyCrmSyncRepository
from app.bridge.crm_sync_worker import CrmSyncData, CrmSyncWorker, NotifyDialogStats
from app.models import (
    KIND_NOTIFY,
    Base,
    Contact,
    CrmSyncItem,
    CrmSyncStatus,
    Dialog,
    DialogNotification,
    Manager,
    Message,
    MessageDirection,
    MessageStatus,
    Messenger,
)


# ---------------------------------------------------------------------- #
# Сборщики уведомления (текст / клавиатура / ссылки / подпись)
# ---------------------------------------------------------------------- #
def test_notification_text_counter_only_when_many():
    text1 = build_notification_text("Telegram", "Иван", "привет", 1)
    assert "привет" in text1 and "Неотвеченных" not in text1
    text3 = build_notification_text("MAX", "Иван", "привет", 3)
    assert "Неотвеченных сообщений: 3" in text3


def test_notification_text_truncated_and_attachment_fallback():
    long = "x" * 500
    text = build_notification_text("Telegram", "Иван", long, 2)
    assert "…" in text and long not in text
    assert "[вложение]" in build_notification_text("Telegram", "Иван", None, 1)
    assert "[вложение]" in build_notification_text("Telegram", "Иван", "  ", 1)


def test_crm_card_url_prefers_entity_then_contact():
    portal = "https://b24.example"
    assert crm_card_url(portal, entity_id=7, entity_type="deal", contact_id=9) == (
        "https://b24.example/crm/deal/7/view/"
    )
    assert crm_card_url(portal, entity_id=7, entity_type="lead", contact_id=None) == (
        "https://b24.example/crm/lead/7/view/"
    )
    assert crm_card_url(portal, entity_id=None, entity_type=None, contact_id=9) == (
        "https://b24.example/crm/contact/9/view/"
    )
    assert crm_card_url(portal, entity_id=None, entity_type=None, contact_id=None) is None


def test_keyboard_buttons_and_empty():
    kb = build_keyboard("https://card", "https://dismiss")
    assert kb == {
        "BUTTONS": [
            {"TEXT": "Открыть диалог", "LINK": "https://card"},
            {"TEXT": "Отвечать не нужно", "LINK": "https://dismiss"},
        ]
    }
    assert build_keyboard(None, None) is None
    assert build_keyboard("https://card", None) == {
        "BUTTONS": [{"TEXT": "Открыть диалог", "LINK": "https://card"}]
    }


def test_dismiss_sign_roundtrip_and_expiry():
    url = sign_dismiss_url("https://app.example/", 55, secret="s", ttl_sec=3600)
    assert url.startswith("https://app.example/notify/dismiss/55/")
    _, _, _, exp, sig = url.rsplit("/", 4)[-5:]
    assert verify_dismiss_sig(55, int(exp), sig, secret="s")
    # Чужой секрет / другой диалог — False.
    assert not verify_dismiss_sig(55, int(exp), sig, secret="evil")
    assert not verify_dismiss_sig(56, int(exp), sig, secret="s")
    # Истёкшая — False.
    assert not verify_dismiss_sig(55, int(time.time()) - 1, sig, secret="s")


# ---------------------------------------------------------------------- #
# Воркер: _handle_notify / clear / sweep
# ---------------------------------------------------------------------- #
def _notify_item(**kw) -> CrmSyncItem:
    defaults = {
        "id": 7,
        "kind": KIND_NOTIFY,
        "message_id": 11,
        "status": CrmSyncStatus.queued,
        "attempts": 0,
        "next_attempt_at": datetime.now(UTC),
        "last_error": None,
    }
    defaults.update(kw)
    return CrmSyncItem(**defaults)


def _notify_data(**kw) -> CrmSyncData:
    defaults = {
        "message_text": "привет",
        "sender_name": "Иван",
        "sender_phone": "+7999",
        "crm_contact_id": 42,
        "crm_entity_id": 100,
        "crm_entity_type": "deal",
        "assigned_b24_user_id": 15,
        "notify_user_ids": [15],
        "messenger": Messenger.tg,
        "dialog_id": 50,
    }
    defaults.update(kw)
    return CrmSyncData(**defaults)


def _stats(**kw) -> NotifyDialogStats:
    defaults = {
        "last_inbound_id": 11,
        "last_inbound_text": "привет",
        "unanswered_count": 1,
    }
    defaults.update(kw)
    return NotifyDialogStats(**defaults)


class _FakeIm:
    """ImService-двойник: журнал send/delete, управляемые сбои."""

    def __init__(self):
        self.sent: list[tuple[int, str, dict | None]] = []
        self.deleted: list[int] = []
        self._next_id = 500
        self.delete_errors: dict[int, Exception] = {}

    async def send_notification(self, auth, user_id, message, keyboard=None):
        self._next_id += 1
        self.sent.append((user_id, message, keyboard))
        return self._next_id

    async def delete_message(self, auth, message_id):
        exc = self.delete_errors.get(message_id)
        if exc is not None:
            raise exc
        self.deleted.append(message_id)


def _token_mgr():
    mgr = AsyncMock()
    token = MagicMock()
    token.access_token = "tok"
    mgr.get_token = AsyncMock(return_value=token)
    return mgr


def _notify_repo(items, data, stats_rows=None, stats=None):
    """AsyncMock-репо с осмысленными return для notify-методов."""
    repo = AsyncMock()
    repo.fetch_due = AsyncMock(return_value=items)
    repo.collect = AsyncMock(return_value=data)
    repo.mark_done = AsyncMock()
    repo.mark_failed = AsyncMock()
    repo.reschedule = AsyncMock()
    repo.get_timeline_mode = AsyncMock(return_value="all")
    repo.get_media_to_timeline = AsyncMock(return_value=False)
    repo.get_crm_mode = AsyncMock(return_value="deal")
    repo.get_source_map = AsyncMock(return_value={})
    repo.has_newer_queued_notify = AsyncMock(return_value=False)
    stats_seq = stats if isinstance(stats, list) else [stats or _stats()] * 10
    repo.notify_dialog_stats = AsyncMock(side_effect=stats_seq)
    repo.notification_rows = AsyncMock(return_value=list(stats_rows or []))
    repo.upsert_notification_rows = AsyncMock()
    repo.remove_notification_row = AsyncMock()
    repo.set_notification_message = AsyncMock()
    repo.pending_dismissed = AsyncMock(return_value=[])
    return repo


@pytest.mark.asyncio
async def test_notify_renders_for_each_recipient():
    """Неотвеченный диалог: delete старой строки + add новой каждому
    адресату, слот запоминает id (repeat-рендер вытеснит его)."""
    row = DialogNotification(
        id=1, dialog_id=50, manager_b24_user_id=15, b24_message_id=777
    )
    fresh_rows = [
        # Слот 15 сохраняет старый id до вытеснения (delete+add).
        DialogNotification(id=1, dialog_id=50, manager_b24_user_id=15, b24_message_id=777),
        DialogNotification(id=2, dialog_id=50, manager_b24_user_id=16, b24_message_id=None),
    ]
    repo = _notify_repo(
        [_notify_item()],
        _notify_data(notify_user_ids=[15, 16]),
        stats_rows=[row],
    )
    repo.notification_rows = AsyncMock(side_effect=[[row], fresh_rows])
    im = _FakeIm()
    worker = CrmSyncWorker(repo=repo, b24sync=AsyncMock(), im=im, token_mgr=_token_mgr())
    await worker._process_once()

    # Старая строка удалена в B24, новая добавлена каждому адресату.
    assert im.deleted == [777]
    assert [uid for uid, _, _ in im.sent] == [15, 16]
    repo.remove_notification_row.assert_not_awaited()  # 15 остался адресатом
    repo.upsert_notification_rows.assert_awaited_once_with(50, [15, 16])
    repo.set_notification_message.assert_any_await(1, 501)
    repo.set_notification_message.assert_any_await(2, 502)
    repo.mark_done.assert_awaited_once()
    # Клавиатура: карточка deal + подписанная ссылка гашения (get_settings
    # из env теста: public_base_url может быть пуст — тогда только карточка).
    _, _, kb = im.sent[0]
    assert kb is not None
    assert kb["BUTTONS"][0]["LINK"].endswith("/crm/deal/100/view/")


@pytest.mark.asyncio
async def test_notify_supersedes_stale_recipient():
    """Адресат ушёл из линии: его сообщение удаляется, слот убирается."""
    stale = DialogNotification(
        id=3, dialog_id=50, manager_b24_user_id=99, b24_message_id=888
    )
    keep = DialogNotification(
        id=1, dialog_id=50, manager_b24_user_id=15, b24_message_id=None
    )
    repo = _notify_repo([_notify_item()], _notify_data(notify_user_ids=[15]), stats_rows=[stale, keep])
    repo.notification_rows = AsyncMock(side_effect=[[stale, keep], [keep]])
    im = _FakeIm()
    worker = CrmSyncWorker(repo=repo, b24sync=AsyncMock(), im=im, token_mgr=_token_mgr())
    await worker._process_once()

    assert im.deleted == [888]
    repo.remove_notification_row.assert_awaited_once_with(3)
    assert [uid for uid, _, _ in im.sent] == [15]


@pytest.mark.asyncio
async def test_notify_postcheck_race_answered_during_render():
    """Ответ прилетел между предикатом и add (пост-проверка из архитектуры
    А): только что добавленные сообщения гасятся, слоты обнуляются."""
    fresh = [
        DialogNotification(id=1, dialog_id=50, manager_b24_user_id=15, b24_message_id=None)
    ]
    repo = _notify_repo(
        [_notify_item()],
        _notify_data(),
        stats_rows=[fresh],
        # До рендера — 2 неотвеченных; после — 0 (ответили).
        stats=[_stats(unanswered_count=2), _stats(unanswered_count=0)],
    )
    repo.notification_rows = AsyncMock(side_effect=[[], fresh])
    im = _FakeIm()
    worker = CrmSyncWorker(repo=repo, b24sync=AsyncMock(), im=im, token_mgr=_token_mgr())
    await worker._process_once()

    assert len(im.sent) == 1
    assert im.deleted == [501]  # добавленное тут же погашено
    repo.set_notification_message.assert_any_await(1, None)
    repo.mark_done.assert_awaited_once()


@pytest.mark.asyncio
async def test_notify_answered_before_processing_only_clears():
    """Ответ успел прийти до обработки item: рендера нет, висящие строки
    гасятся (delete + null)."""
    row = DialogNotification(
        id=1, dialog_id=50, manager_b24_user_id=15, b24_message_id=777
    )
    repo = _notify_repo(
        [_notify_item()], _notify_data(), stats_rows=[row],
        stats=_stats(unanswered_count=0),
    )
    im = _FakeIm()
    worker = CrmSyncWorker(repo=repo, b24sync=AsyncMock(), im=im, token_mgr=_token_mgr())
    await worker._process_once()

    assert im.sent == []
    assert im.deleted == [777]
    repo.set_notification_message.assert_any_await(1, None)
    repo.mark_done.assert_awaited_once()


@pytest.mark.asyncio
async def test_notify_burst_collapsed_by_newer_item():
    repo = _notify_repo([_notify_item()], _notify_data())
    repo.has_newer_queued_notify = AsyncMock(return_value=True)
    im = _FakeIm()
    worker = CrmSyncWorker(repo=repo, b24sync=AsyncMock(), im=im, token_mgr=_token_mgr())
    await worker._process_once()

    assert im.sent == []
    repo.mark_done.assert_awaited_once()


@pytest.mark.asyncio
async def test_notify_ol_dialog_done_without_render():
    repo = _notify_repo([_notify_item()], _notify_data(ol_line_id="107"))
    im = _FakeIm()
    worker = CrmSyncWorker(repo=repo, b24sync=AsyncMock(), im=im, token_mgr=_token_mgr())
    await worker._process_once()

    assert im.sent == []
    repo.mark_done.assert_awaited_once()


@pytest.mark.asyncio
async def test_notify_without_im_service_marks_done():
    """im не сконфигурирован (тесты без сервиса) — ветка выключена."""
    repo = _notify_repo([_notify_item()], _notify_data())
    worker = CrmSyncWorker(repo=repo, b24sync=AsyncMock())
    await worker._process_once()
    repo.mark_done.assert_awaited_once()


@pytest.mark.asyncio
async def test_notify_no_token_is_retryable():
    repo = _notify_repo([_notify_item()], _notify_data())
    mgr = AsyncMock()
    mgr.get_token = AsyncMock(return_value=None)
    worker = CrmSyncWorker(repo=repo, b24sync=AsyncMock(), im=_FakeIm(), token_mgr=mgr)
    await worker._process_once()

    repo.mark_done.assert_not_awaited()
    assert repo.reschedule.call_args.kwargs["error"] == "no_b24_token"


@pytest.mark.asyncio
async def test_notify_delete_degradation_on_bitrix_error():
    """CANT_EDIT_MESSAGE/«не найдено» — деградация: слот гасится, рендер
    продолжается (сетевые ошибки, наоборот, ретраятся — отдельный тест)."""
    row = DialogNotification(
        id=1, dialog_id=50, manager_b24_user_id=15, b24_message_id=777
    )
    repo = _notify_repo([_notify_item()], _notify_data(), stats_rows=[row])
    repo.notification_rows = AsyncMock(side_effect=[[row], [row]])
    im = _FakeIm()
    im.delete_errors[777] = Bitrix24Error("CANT_EDIT_MESSAGE", "окно истекло")
    worker = CrmSyncWorker(repo=repo, b24sync=AsyncMock(), im=im, token_mgr=_token_mgr())
    await worker._process_once()

    assert len(im.sent) == 1  # рендер не упал
    repo.set_notification_message.assert_awaited_once_with(1, 501)
    repo.mark_done.assert_awaited_once()


@pytest.mark.asyncio
async def test_outbound_clears_notifications_before_timeline():
    """Не-автоответ: строки диалога гасятся ДО timeline-комментария (ретрай
    без дублей комментария)."""
    row = DialogNotification(
        id=1, dialog_id=50, manager_b24_user_id=15, b24_message_id=777
    )
    repo = _notify_repo(
        [_notify_item(id=8, kind="outbound")], _notify_data(), stats_rows=[row]
    )
    sync = AsyncMock()
    sync.process_outbound = AsyncMock(return_value=555)
    im = _FakeIm()
    worker = CrmSyncWorker(repo=repo, b24sync=sync, im=im, token_mgr=_token_mgr())
    await worker._process_once()

    assert im.deleted == [777]
    repo.set_notification_message.assert_any_await(1, None)
    sync.process_outbound.assert_awaited_once()
    repo.mark_done.assert_awaited_once()


@pytest.mark.asyncio
async def test_outbound_autoreply_does_not_clear():
    repo = _notify_repo(
        [_notify_item(id=8, kind="outbound")], _notify_data(is_autoreply=True)
    )
    sync = AsyncMock()
    im = _FakeIm()
    worker = CrmSyncWorker(repo=repo, b24sync=sync, im=im, token_mgr=_token_mgr())
    await worker._process_once()

    repo.notification_rows.assert_not_awaited()
    assert im.deleted == []
    repo.mark_done.assert_awaited_once()


@pytest.mark.asyncio
async def test_sweep_dismissed_deletes_and_nulls():
    """«Отвечать не нужно»: web поставил dismissed_at — sweep удаляет
    сообщение и обнуляет слот."""
    row = DialogNotification(
        id=2, dialog_id=50, manager_b24_user_id=15, b24_message_id=999,
        dismissed_at=datetime.now(UTC),
    )
    repo = _notify_repo([], None)
    repo.pending_dismissed = AsyncMock(return_value=[row])
    im = _FakeIm()
    worker = CrmSyncWorker(repo=repo, b24sync=AsyncMock(), im=im, token_mgr=_token_mgr())
    await worker._process_once()

    assert im.deleted == [999]
    repo.set_notification_message.assert_any_await(2, None)


@pytest.mark.asyncio
async def test_inbound_enqueues_notify_item():
    """После классического inbound-синка ставится notify-item (OL-ветка —
    раньше return, туда не доходит)."""
    from app.b24.sync import SyncResult

    repo = _notify_repo([_notify_item(id=5, kind="inbound")], _notify_data())
    sync = AsyncMock()
    sync.process_inbound = AsyncMock(
        return_value=SyncResult(crm_entity_type="deal", crm_entity_id=100, contact_id=42, is_new=True)
    )
    worker = CrmSyncWorker(repo=repo, b24sync=sync, max_attempts=5)
    await worker._process_once()

    repo.enqueue.assert_awaited_once_with(kind=KIND_NOTIFY, message_id=11)


# ---------------------------------------------------------------------- #
# Репозиторий (SQLite): предикат, слоты, dismissed, схлопывание
# ---------------------------------------------------------------------- #
@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with SessionLocal() as s:
        yield s
    await engine.dispose()


async def _seed_dialog(session) -> int:
    session.add(Manager(id=1, name="Менеджер", b24_user_id=15))
    session.add(Contact(id=10, messenger=Messenger.tg, external_user_id="u", name="Иван"))
    session.add(
        Dialog(id=50, contact_id=10, messenger=Messenger.tg, external_chat_id="c")
    )
    await session.flush()
    return 50


async def _add_msg(session, dialog_id, direction, *, autoreply=False, text="m") -> int:
    status = MessageStatus.sent if direction == MessageDirection.outbound else MessageStatus.delivered
    msg = Message(
        dialog_id=dialog_id,
        direction=direction,
        status=status,
        is_autoreply=autoreply,
        text=text,
    )
    session.add(msg)
    await session.flush()
    return msg.id


@pytest.mark.asyncio
async def test_stats_predicate_ignores_autoreply(session):
    """Автоответ не снимает неотвеченность (семантика Wazzup); живой ответ
    снимает; счётчик считает входящие после последнего живого ответа."""
    dialog_id = await _seed_dialog(session)
    await _add_msg(session, dialog_id, MessageDirection.inbound)
    await _add_msg(session, dialog_id, MessageDirection.outbound, autoreply=True)
    await _add_msg(session, dialog_id, MessageDirection.inbound)

    repo = SqlAlchemyCrmSyncRepository(session)
    stats = await repo.notify_dialog_stats(dialog_id)
    assert stats.unanswered_count == 2  # автоответ не считается ответом

    await _add_msg(session, dialog_id, MessageDirection.outbound)
    stats = await repo.notify_dialog_stats(dialog_id)
    assert stats.unanswered_count == 0
    assert stats.last_inbound_id is not None


@pytest.mark.asyncio
async def test_stats_empty_dialog(session):
    dialog_id = await _seed_dialog(session)
    stats = await SqlAlchemyCrmSyncRepository(session).notify_dialog_stats(dialog_id)
    assert stats.last_inbound_id is None and stats.unanswered_count == 0


@pytest.mark.asyncio
async def test_upsert_rows_idempotent_and_unique(session):
    dialog_id = await _seed_dialog(session)
    repo = SqlAlchemyCrmSyncRepository(session)
    await repo.upsert_notification_rows(dialog_id, [15, 16])
    await repo.upsert_notification_rows(dialog_id, [15, 16])  # идемпо

    rows = await repo.notification_rows(dialog_id)
    assert sorted(r.manager_b24_user_id for r in rows) == [15, 16]

    await repo.set_notification_message(rows[0].id, 777)
    await repo.remove_notification_row(rows[1].id)
    rows = await repo.notification_rows(dialog_id)
    assert len(rows) == 1 and rows[0].b24_message_id == 777

    await repo.set_notification_message(rows[0].id, None)
    assert (await repo.notification_rows(dialog_id))[0].b24_message_id is None


@pytest.mark.asyncio
async def test_dismiss_marks_and_pending_dismissed(session):
    dialog_id = await _seed_dialog(session)
    repo = SqlAlchemyCrmSyncRepository(session)
    await repo.upsert_notification_rows(dialog_id, [15])
    row = (await repo.notification_rows(dialog_id))[0]

    assert await repo.pending_dismissed() == []  # без сообщения не доехал
    await repo.set_notification_message(row.id, 888)
    await session.execute(
        update(DialogNotification)
        .where(DialogNotification.dialog_id == dialog_id)
        .values(dismissed_at=datetime.now(UTC))
    )
    await session.commit()

    pending = await repo.pending_dismissed()
    assert len(pending) == 1 and pending[0].b24_message_id == 888
    # Рендер сбрасывает dismissed_at — sweep не снесёт свежее сообщение.
    await repo.set_notification_message(row.id, 889)
    assert await repo.pending_dismissed() == []


@pytest.mark.asyncio
async def test_has_newer_queued_notify(session):
    dialog_id = await _seed_dialog(session)
    m1 = await _add_msg(session, dialog_id, MessageDirection.inbound)
    m2 = await _add_msg(session, dialog_id, MessageDirection.inbound)
    repo = SqlAlchemyCrmSyncRepository(session)
    first = await repo.enqueue(kind=KIND_NOTIFY, message_id=m1)
    await session.commit()
    assert await repo.has_newer_queued_notify(first.id, dialog_id) is False
    second = await repo.enqueue(kind=KIND_NOTIFY, message_id=m2)
    await session.commit()
    assert await repo.has_newer_queued_notify(first.id, dialog_id) is True
    assert await repo.has_newer_queued_notify(second.id, dialog_id) is False


# ---------------------------------------------------------------------- #
# Роут /notify/dismiss (публичный, подписанный URL)
# ---------------------------------------------------------------------- #
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
    yield factory
    await engine.dispose()


@pytest.fixture
def client(db, monkeypatch):
    import asyncio

    async def seed():
        async with db() as s:
            s.add(Contact(id=10, messenger=Messenger.tg, external_user_id="u", name="И"))
            s.add(Dialog(id=55, contact_id=10, messenger=Messenger.tg, external_chat_id="c"))
            s.add(
                DialogNotification(
                    id=1, dialog_id=55, manager_b24_user_id=15, b24_message_id=777
                )
            )
            await s.commit()

    asyncio.run(seed())
    monkeypatch.setattr("app.db.async_session", db)
    from app.web.app import create_app

    return TestClient(create_app())


def _dismiss_path(dialog_id=55, secret="test-session-secret", ttl=3600) -> str:
    url = sign_dismiss_url("https://x", dialog_id, secret=secret, ttl_sec=ttl)
    return "/" + url.split("://x/", 1)[1]


def test_dismiss_route_marks_dialog(client, db):
    resp = client.get(_dismiss_path())
    assert resp.status_code == 200
    assert "погашено" in resp.text

    async def check():
        async with db() as s:
            row = (
                await s.execute(select(DialogNotification).where(DialogNotification.id == 1))
            ).scalar_one()
            assert row.dismissed_at is not None

    import asyncio

    asyncio.run(check())


def test_dismiss_route_bad_signature_404(client):
    assert client.get(_dismiss_path(secret="evil")).status_code == 404


def test_dismiss_route_expired_404(client):
    assert client.get(_dismiss_path(ttl=-10)).status_code == 404


def test_dismiss_route_unknown_dialog_404(client):
    assert client.get(_dismiss_path(dialog_id=404)).status_code == 404
