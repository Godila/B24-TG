"""CrmSyncWorker: mock-repo + mock-sync — успех/провал/backoff/терминал."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from app.b24.sync import SyncResult
from app.bridge.crm_sync_worker import (
    AttachmentMeta,
    CrmSyncData,
    CrmSyncWorker,
)
from app.models import KIND_INBOUND, KIND_OUTBOUND, CrmSyncItem, CrmSyncStatus, Messenger


def _make_item(**kw) -> CrmSyncItem:
    defaults = {
        "id": 1,
        "kind": KIND_INBOUND,
        "message_id": 11,
        "status": CrmSyncStatus.queued,
        "attempts": 0,
        "next_attempt_at": datetime.now(UTC),
        "last_error": None,
    }
    defaults.update(kw)
    return CrmSyncItem(**defaults)


def _make_data(**kw) -> CrmSyncData:
    defaults = {
        "message_text": "Привет",
        "sender_name": "Иван",
        "sender_phone": "+79991234567",
        "crm_contact_id": 42,
        "crm_entity_id": 100,
        "crm_entity_type": "deal",
        "assigned_b24_user_id": 15,
        "notify_user_ids": [15],
        "messenger": Messenger.tg,
    }
    defaults.update(kw)
    return CrmSyncData(**defaults)


def _make_repo(items, data) -> AsyncMock:
    repo = AsyncMock()
    repo.fetch_due = AsyncMock(return_value=items)
    repo.collect = AsyncMock(return_value=data)
    repo.mark_done = AsyncMock()
    repo.mark_failed = AsyncMock()
    repo.reschedule = AsyncMock()
    repo.apply_inbound_result = AsyncMock()
    repo.set_timeline_comment = AsyncMock()
    repo.get_timeline_mode = AsyncMock(return_value="all")
    repo.get_media_to_timeline = AsyncMock(return_value=False)
    repo.get_crm_mode = AsyncMock(return_value="deal")
    repo.get_source_map = AsyncMock(return_value={})
    return repo


# ---------------------------------------------------------------------- #
# kind=inbound
# ---------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_inbound_success_applies_result_and_marks_done():
    repo = _make_repo([_make_item()], _make_data())
    sync = AsyncMock()
    sync.process_inbound = AsyncMock(
        return_value=SyncResult(
            crm_entity_type="deal",
            crm_entity_id=100,
            contact_id=42,
            is_new=True,
            timeline_comment_id=999,
        )
    )
    worker = CrmSyncWorker(repo=repo, b24sync=sync, max_attempts=5)
    await worker._process_once()

    sync.process_inbound.assert_awaited_once()
    call = sync.process_inbound.call_args.kwargs
    assert call["sender_name"] == "Иван"
    assert call["sender_phone"] == "+79991234567"
    assert call["message_text"] == "Привет"
    assert call["assigned_b24_user_id"] == 15
    repo.apply_inbound_result.assert_awaited_once_with(
        11,
        contact_id=42,
        crm_entity_type="deal",
        crm_entity_id=100,
        timeline_comment_id=999,
    )
    repo.mark_done.assert_awaited_once()
    repo.reschedule.assert_not_awaited()
    repo.mark_failed.assert_not_awaited()


@pytest.mark.asyncio
async def test_inbound_failure_reschedules_with_backoff():
    repo = _make_repo([_make_item()], _make_data())
    sync = AsyncMock()
    sync.process_inbound = AsyncMock(side_effect=RuntimeError("b24 down"))

    worker = CrmSyncWorker(repo=repo, b24sync=sync, max_attempts=5)
    await worker._process_once()

    repo.mark_done.assert_not_awaited()
    repo.reschedule.assert_awaited_once()
    _, kwargs = repo.reschedule.call_args
    assert kwargs["delay_seconds"] == 30  # 30 * 2^0
    assert "b24 down" in kwargs["error"]


@pytest.mark.asyncio
async def test_retry_failure_logs_warning(caplog):
    """Инцидент 2026-08-17 (LAST_NAME=null): сбои были тихи в логах — теперь
    каждая ретраибельная попытка пишет WARNING (видно без залезания в БД)."""
    import logging

    repo = _make_repo([_make_item()], _make_data())
    sync = AsyncMock()
    sync.process_inbound = AsyncMock(side_effect=RuntimeError("b24 down"))

    worker = CrmSyncWorker(repo=repo, b24sync=sync, max_attempts=5)
    with caplog.at_level(logging.WARNING, logger="app.bridge.crm_sync_worker"):
        await worker._process_once()

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "attempt 1/5" in message
    assert "retry" in message and "30" in message
    assert "b24 down" in message


@pytest.mark.asyncio
async def test_inbound_failure_exponential_backoff():
    repo = _make_repo([_make_item(attempts=2)], _make_data())
    sync = AsyncMock()
    sync.process_inbound = AsyncMock(side_effect=RuntimeError("x"))

    worker = CrmSyncWorker(repo=repo, b24sync=sync, max_attempts=5)
    await worker._process_once()

    _, kwargs = repo.reschedule.call_args
    assert kwargs["delay_seconds"] == 120  # 30 * 2^2


@pytest.mark.asyncio
async def test_inbound_max_attempts_marks_failed():
    """5-я попытка (attempts=4) исчерпала лимит — терминальный failed."""
    repo = _make_repo([_make_item(attempts=4)], _make_data())
    sync = AsyncMock()
    sync.process_inbound = AsyncMock(side_effect=RuntimeError("still down"))

    worker = CrmSyncWorker(repo=repo, b24sync=sync, max_attempts=5)
    await worker._process_once()

    repo.mark_failed.assert_awaited_once()
    assert repo.mark_failed.call_args.args[1] == "still down"
    repo.reschedule.assert_not_awaited()


@pytest.mark.asyncio
async def test_inbound_no_token_is_retryable():
    """process_inbound -> None (нет B24-токена) — ретраибельная ошибка,
    а не молчаливая потеря CRM-записи."""
    repo = _make_repo([_make_item()], _make_data())
    sync = AsyncMock()
    sync.process_inbound = AsyncMock(return_value=None)

    worker = CrmSyncWorker(repo=repo, b24sync=sync, max_attempts=5)
    await worker._process_once()

    repo.mark_done.assert_not_awaited()
    repo.reschedule.assert_awaited_once()
    assert repo.reschedule.call_args.kwargs["error"] == "no_b24_token"


@pytest.mark.asyncio
async def test_inbound_message_not_found_is_terminal():
    repo = _make_repo([_make_item()], None)
    sync = AsyncMock()

    worker = CrmSyncWorker(repo=repo, b24sync=sync, max_attempts=5)
    await worker._process_once()

    sync.process_inbound.assert_not_awaited()
    repo.mark_failed.assert_awaited_once()
    assert repo.mark_failed.call_args.args[1] == "message_not_found"


@pytest.mark.asyncio
async def test_inbound_no_assigned_manager_passes_none():
    """Общий номер без ответственного: CRM без ASSIGNED_BY_ID, уведомления —
    по списку участников (собирает collect); терминального фейла нет."""
    repo = _make_repo(
        [_make_item()],
        _make_data(assigned_b24_user_id=None, notify_user_ids=[21, 22]),
    )
    sync = AsyncMock()

    worker = CrmSyncWorker(repo=repo, b24sync=sync, max_attempts=5)
    await worker._process_once()

    call = sync.process_inbound.call_args.kwargs
    assert call["assigned_b24_user_id"] is None
    assert call["notify_user_ids"] == [21, 22]
    repo.mark_done.assert_awaited_once()


# ---------------------------------------------------------------------- #
# kind=outbound
# ---------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_outbound_success_sets_comment_and_marks_done():
    repo = _make_repo([_make_item(kind=KIND_OUTBOUND)], _make_data(message_text="Ответ менеджера"))
    sync = AsyncMock()
    sync.process_outbound = AsyncMock(return_value=555)

    worker = CrmSyncWorker(repo=repo, b24sync=sync, max_attempts=5)
    await worker._process_once()

    sync.process_outbound.assert_awaited_once_with(
        dialog_entity_id=100,
        dialog_entity_type="deal",
        contact_id=42,
        text="Ответ менеджера",
        timeline_mode="all",
        files=[],
    )
    repo.set_timeline_comment.assert_awaited_once_with(11, 555)
    repo.mark_done.assert_awaited_once()


@pytest.mark.asyncio
async def test_outbound_without_entity_done_without_comment():
    """Ни сделки, ни контакта — писать некуда: done без timeline_comment_id
    (для исходящих это не ошибка)."""
    repo = _make_repo(
        [_make_item(kind=KIND_OUTBOUND)],
        _make_data(crm_entity_id=None, crm_entity_type=None, crm_contact_id=None),
    )
    sync = AsyncMock()
    sync.process_outbound = AsyncMock(return_value=None)

    worker = CrmSyncWorker(repo=repo, b24sync=sync, max_attempts=5)
    await worker._process_once()

    repo.set_timeline_comment.assert_not_awaited()
    repo.mark_done.assert_awaited_once()


@pytest.mark.asyncio
async def test_outbound_failure_reschedules():
    repo = _make_repo([_make_item(kind=KIND_OUTBOUND, attempts=1)], _make_data())
    sync = AsyncMock()
    sync.process_outbound = AsyncMock(side_effect=RuntimeError("network"))

    worker = CrmSyncWorker(repo=repo, b24sync=sync, max_attempts=5)
    await worker._process_once()

    repo.mark_done.assert_not_awaited()
    repo.reschedule.assert_awaited_once()
    _, kwargs = repo.reschedule.call_args
    assert kwargs["delay_seconds"] == 60  # 30 * 2^1
    assert "network" in kwargs["error"]


@pytest.mark.asyncio
async def test_outbound_terminal_after_max_attempts():
    repo = _make_repo([_make_item(kind=KIND_OUTBOUND, attempts=4)], _make_data())
    sync = AsyncMock()
    sync.process_outbound = AsyncMock(side_effect=RuntimeError("5xx"))

    worker = CrmSyncWorker(repo=repo, b24sync=sync, max_attempts=5)
    await worker._process_once()

    repo.mark_failed.assert_awaited_once()
    repo.reschedule.assert_not_awaited()


@pytest.mark.asyncio
async def test_worker_passes_timeline_mode_to_sync():
    """Режим из repo.get_timeline_mode() доезжает до process_inbound."""
    data = _make_data(message_text="вопрос")
    repo = _make_repo([_make_item(kind=KIND_INBOUND)], data)
    repo.get_timeline_mode = AsyncMock(return_value="first")
    sync = AsyncMock()
    sync.process_inbound = AsyncMock(
        return_value=SyncResult(crm_entity_type="deal", crm_entity_id=100, contact_id=42, is_new=True)
    )

    worker = CrmSyncWorker(repo=repo, b24sync=sync, max_attempts=5)
    await worker._process_once()

    assert sync.process_inbound.call_args.kwargs.get("timeline_mode") == "first"


@pytest.mark.asyncio
async def test_inbound_passes_channel_profile_fields():
    """Split-имя и @username из CrmSyncData доходят до process_inbound."""
    data = _make_data(
        sender_first_name="Иван",
        sender_last_name="Петров",
        sender_username="ivan_p",
    )
    repo = _make_repo([_make_item()], data)
    sync = AsyncMock()
    sync.process_inbound = AsyncMock(
        return_value=SyncResult(crm_entity_type="deal", crm_entity_id=100, contact_id=42, is_new=True)
    )
    worker = CrmSyncWorker(repo=repo, b24sync=sync, max_attempts=5)
    await worker._process_once()

    call = sync.process_inbound.call_args.kwargs
    assert call["sender_first_name"] == "Иван"
    assert call["sender_last_name"] == "Петров"
    assert call["sender_username"] == "ivan_p"


@pytest.mark.asyncio
async def test_worker_passes_crm_mode_and_entity_binding():
    """crm_mode из repo.get_crm_mode() + тип/id сущности из collect доезжают
    до process_inbound; тип результата — до apply_inbound_result."""
    data = _make_data(crm_entity_id=55, crm_entity_type="lead")
    repo = _make_repo([_make_item()], data)
    repo.get_crm_mode = AsyncMock(return_value="lead")
    sync = AsyncMock()
    sync.process_inbound = AsyncMock(
        return_value=SyncResult(
            crm_entity_type="lead", crm_entity_id=55, contact_id=None, is_new=True
        )
    )
    worker = CrmSyncWorker(repo=repo, b24sync=sync, max_attempts=5)
    await worker._process_once()

    call = sync.process_inbound.call_args.kwargs
    assert call["crm_mode"] == "lead"
    assert call["existing_entity_id"] == 55
    assert call["existing_entity_type"] == "lead"
    repo.apply_inbound_result.assert_awaited_once_with(
        11,
        contact_id=None,
        crm_entity_type="lead",
        crm_entity_id=55,
        timeline_comment_id=None,
    )


@pytest.mark.asyncio
async def test_worker_passes_source_map_to_sync():
    """Маппинг источников из repo.get_source_map() доезжает до process_inbound."""
    data = _make_data()
    repo = _make_repo([_make_item()], data)
    repo.get_source_map = AsyncMock(return_value={Messenger.tg: "CALL"})
    sync = AsyncMock()
    sync.process_inbound = AsyncMock(
        return_value=SyncResult(crm_entity_type="deal", crm_entity_id=100, contact_id=42, is_new=True)
    )

    worker = CrmSyncWorker(repo=repo, b24sync=sync, max_attempts=5)
    await worker._process_once()

    assert sync.process_inbound.call_args.kwargs.get("source_map") == {Messenger.tg: "CALL"}


# ---------------------------------------------------------------------- #
# Медиа в timeline-комментарии (app_settings.media_to_timeline)
# ---------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_inbound_media_files_attached_when_enabled(tmp_path):
    """Настройка вкл + файл на томе → process_inbound получает FILES-payload
    [(имя, base64)] — в карточку CRM попадает сам файл."""
    import base64 as b64mod

    from app.media.storage import MediaStorage

    storage = MediaStorage(tmp_path)
    absolute, relative = storage.new_path(direction="in", ext="jpg")
    absolute.write_bytes(b"IMGDATA")

    data = _make_data(
        attachments=[
            AttachmentMeta(
                file_path=relative,
                file_name="photo.jpg",
                mime_type="image/jpeg",
                size=7,
            )
        ]
    )
    repo = _make_repo([_make_item()], data)
    repo.get_media_to_timeline = AsyncMock(return_value=True)
    sync = AsyncMock()

    worker = CrmSyncWorker(repo=repo, b24sync=sync, media_storage=storage)
    await worker._process_once()

    kwargs = sync.process_inbound.await_args.kwargs
    assert kwargs["files"] == [("photo.jpg", b64mod.b64encode(b"IMGDATA").decode())]


@pytest.mark.asyncio
async def test_inbound_media_files_empty_when_disabled(tmp_path):
    from app.media.storage import MediaStorage

    data = _make_data(attachments=[AttachmentMeta(file_path="in/x.jpg", size=1)])
    repo = _make_repo([_make_item()], data)
    repo.get_media_to_timeline = AsyncMock(return_value=False)
    sync = AsyncMock()

    worker = CrmSyncWorker(repo=repo, b24sync=sync, media_storage=MediaStorage(tmp_path))
    await worker._process_once()

    assert sync.process_inbound.await_args.kwargs["files"] == []


@pytest.mark.asyncio
async def test_inbound_media_oversize_skipped(tmp_path):
    """Файл больше лимита не грузится в B24 — комментарий остаётся с
    текст-меткой (сбой файла не роняет CRM-запись)."""
    from app.media.storage import MediaStorage

    storage = MediaStorage(tmp_path)
    absolute, relative = storage.new_path(direction="in", ext="bin")
    absolute.write_bytes(b"0123456789")

    data = _make_data(
        attachments=[AttachmentMeta(file_path=relative, file_name="big.bin", size=10)]
    )
    repo = _make_repo([_make_item()], data)
    repo.get_media_to_timeline = AsyncMock(return_value=True)
    sync = AsyncMock()

    worker = CrmSyncWorker(
        repo=repo, b24sync=sync, media_storage=storage, media_timeline_max_bytes=4
    )
    await worker._process_once()

    assert sync.process_inbound.await_args.kwargs["files"] == []


@pytest.mark.asyncio
async def test_inbound_media_missing_file_skipped(tmp_path):
    from app.media.storage import MediaStorage

    data = _make_data(
        attachments=[AttachmentMeta(file_path="in/gone.jpg", file_name="g.jpg", size=1)]
    )
    repo = _make_repo([_make_item()], data)
    repo.get_media_to_timeline = AsyncMock(return_value=True)
    sync = AsyncMock()

    worker = CrmSyncWorker(repo=repo, b24sync=sync, media_storage=MediaStorage(tmp_path))
    await worker._process_once()

    assert sync.process_inbound.await_args.kwargs["files"] == []
    repo.mark_done.assert_awaited_once()


# ---------------------------------------------------------------------- #
# Открытые линии (imconnector): ветки воркера вместо CRM-синка
# ---------------------------------------------------------------------- #

from app.b24.client import Bitrix24Error


class _FakeOpenLine:
    def __init__(self, send_result=True, send_exc=None):
        self.send_calls: list = []
        self.delivery_calls: list = []
        self._send_result = send_result
        self._send_exc = send_exc

    async def send_messages(self, *, line_id, messages):
        self.send_calls.append((line_id, messages))
        if self._send_exc is not None:
            raise self._send_exc
        return self._send_result

    async def send_status_delivery(self, *, line_id, messages):
        self.delivery_calls.append((line_id, messages))
        return self._send_result


def _ol_data(**kw):
    defaults = {
        "message_text": "Привет из TG",
        "sender_name": "Иван",
        "sender_phone": "+7999",
        "messenger": Messenger.tg,
        "dialog_id": 11,
        "chat_title": None,
        "external_message_id": "991",
        "message_created_at": datetime(2026, 8, 20, 12, 0, tzinfo=UTC),
        "sent_at": None,
        "contact_external_user_id": "u50",
        "ol_line_id": "107",
        "ol_active": True,
    }
    defaults.update(kw)
    return _make_data(**defaults)


@pytest.mark.asyncio
async def test_ol_inbound_sends_to_line_instead_of_crm():
    repo = _make_repo([_make_item()], _ol_data())
    sync = AsyncMock()
    ol = _FakeOpenLine()
    worker = CrmSyncWorker(repo=repo, b24sync=sync, openline=ol)
    await worker._process_once()

    sync.process_inbound.assert_not_awaited()
    assert len(ol.send_calls) == 1
    line_id, messages = ol.send_calls[0]
    assert line_id == "107"
    (msg,) = messages
    assert msg["chat"]["id"] == "11"
    assert msg["user"]["id"] == "tg_u50"
    assert msg["message"]["id"] == "991"
    assert msg["message"]["text"] == "Привет из TG"
    repo.mark_done.assert_awaited_once()


@pytest.mark.asyncio
async def test_ol_inbound_inactive_waits_without_burning_attempts():
    repo = _make_repo([_make_item()], _ol_data(ol_active=False))
    ol = _FakeOpenLine()
    worker = CrmSyncWorker(repo=repo, b24sync=AsyncMock(), openline=ol)
    await worker._process_once()

    assert ol.send_calls == []
    repo.reschedule.assert_awaited_once()
    kwargs = repo.reschedule.call_args.kwargs
    assert kwargs["delay_seconds"] == 300
    assert kwargs["count_attempt"] is False
    repo.mark_done.assert_not_awaited()


@pytest.mark.asyncio
async def test_ol_inbound_not_active_line_waits_without_burning_attempts():
    repo = _make_repo([_make_item()], _ol_data())
    ol = _FakeOpenLine(send_exc=Bitrix24Error("NOT_ACTIVE_LINE", "линия выключена"))
    worker = CrmSyncWorker(repo=repo, b24sync=AsyncMock(), openline=ol)
    await worker._process_once()

    repo.reschedule.assert_awaited_once()
    assert repo.reschedule.call_args.kwargs["count_attempt"] is False
    repo.mark_failed.assert_not_awaited()


@pytest.mark.asyncio
async def test_ol_inbound_no_token_retries_with_attempt():
    repo = _make_repo([_make_item()], _ol_data())
    ol = _FakeOpenLine(send_result=None)
    worker = CrmSyncWorker(repo=repo, b24sync=AsyncMock(), openline=ol)
    await worker._process_once()

    repo.reschedule.assert_awaited_once()
    # count_attempt не передан = дефолт True (burn), unlike ol_inactive-ветки
    assert "count_attempt" not in repo.reschedule.call_args.kwargs
    assert "no_b24_token" in repo.reschedule.call_args.kwargs["error"]


@pytest.mark.asyncio
async def test_ol_inbound_files_signed_urls(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://app.example")
    from app.config import get_settings

    get_settings.cache_clear()
    try:
        repo = _make_repo(
            [_make_item()],
            _ol_data(
                attachments=[
                    AttachmentMeta(
                        file_path="in/x.png",
                        file_name="foto.png",
                        mime_type="image/png",
                        size=10,
                        attachment_id=33,
                    )
                ]
            ),
        )
        ol = _FakeOpenLine()
        worker = CrmSyncWorker(repo=repo, b24sync=AsyncMock(), openline=ol)
        await worker._process_once()

        (msg,) = ol.send_calls[0][1]
        (f,) = msg["message"]["files"]
        assert f["name"] == "foto.png"
        assert f["url"].startswith("https://app.example/media/public/33/")
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_ol_inbound_files_skipped_without_public_base_url(monkeypatch):
    # Герметичность: прод-контейнер несёт PUBLIC_BASE_URL в env — убираем.
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    from app.config import get_settings

    get_settings.cache_clear()
    try:
        repo = _make_repo(
            [_make_item()],
            _ol_data(attachments=[AttachmentMeta(file_path="in/x.png", attachment_id=33)]),
        )
        ol = _FakeOpenLine()
        worker = CrmSyncWorker(repo=repo, b24sync=AsyncMock(), openline=ol)
        await worker._process_once()

        (msg,) = ol.send_calls[0][1]
        assert "files" not in msg["message"]
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_ol_outbound_sends_delivery_status():
    repo = _make_repo(
        [_make_item(kind=KIND_OUTBOUND)],
        _ol_data(
            b24_im_chat_id=1807,
            b24_im_message_id=86497,
            sent_at=datetime(2026, 8, 20, 12, 0, tzinfo=UTC),
        ),
    )
    sync = AsyncMock()
    ol = _FakeOpenLine()
    worker = CrmSyncWorker(repo=repo, b24sync=sync, openline=ol)
    await worker._process_once()

    sync.process_outbound.assert_not_awaited()
    assert len(ol.delivery_calls) == 1
    line_id, messages = ol.delivery_calls[0]
    assert line_id == "107"
    (m,) = messages
    assert m["im"] == {"chat_id": 1807, "message_id": 86497}
    assert m["chat"]["id"] == "11"
    repo.mark_done.assert_awaited_once()


@pytest.mark.asyncio
async def test_ol_outbound_without_im_pair_mirrors_to_line():
    """Панель/БП/инициация: im-пар нет — зеркало в чат линии send.messages
    (user = клиент — тред сохраняется; автор в префиксе текста)."""
    repo = _make_repo(
        [_make_item(kind=KIND_OUTBOUND)],
        _ol_data(author_name="Иван", sent_at=datetime(2026, 8, 20, 12, 0, tzinfo=UTC)),
    )
    ol = _FakeOpenLine()
    worker = CrmSyncWorker(repo=repo, b24sync=AsyncMock(), openline=ol)
    await worker._process_once()

    assert ol.delivery_calls == []
    assert len(ol.send_calls) == 1
    line_id, messages = ol.send_calls[0]
    assert line_id == "107"
    (m,) = messages
    assert m["user"]["id"] == "tg_u50"  # клиент — тред чата не форкается
    assert m["message"]["id"] == "991"
    assert m["message"]["text"] == "↗️ Исходящее (ЧатМост, Иван): Привет из TG"
    repo.mark_done.assert_awaited_once()


@pytest.mark.asyncio
async def test_ol_outbound_mirror_disabled_done():
    """Тумблер ol_panel_mirror=off — зеркало выключено, done без действий."""
    repo = _make_repo([_make_item(kind=KIND_OUTBOUND)], _ol_data())
    repo.get_ol_panel_mirror = AsyncMock(return_value=False)
    ol = _FakeOpenLine()
    worker = CrmSyncWorker(repo=repo, b24sync=AsyncMock(), openline=ol)
    await worker._process_once()

    assert ol.send_calls == [] and ol.delivery_calls == []
    repo.mark_done.assert_awaited_once()


@pytest.mark.asyncio
async def test_ol_outbound_mirror_inactive_line_waits_without_burn():
    """Коннектор деактивирован: панельное исходящее ждёт реактивации
    (как inbound) — перепривязка вернёт его в классический CRM-синк."""
    repo = _make_repo([_make_item(kind=KIND_OUTBOUND)], _ol_data(ol_active=False))
    ol = _FakeOpenLine()
    worker = CrmSyncWorker(repo=repo, b24sync=AsyncMock(), openline=ol)
    await worker._process_once()

    assert ol.send_calls == []
    repo.reschedule.assert_awaited_once()
    assert repo.reschedule.call_args.kwargs["count_attempt"] is False


@pytest.mark.asyncio
async def test_ol_outbound_not_active_line_loses_status_but_done():
    repo = _make_repo(
        [_make_item(kind=KIND_OUTBOUND)],
        _ol_data(b24_im_chat_id=1807, b24_im_message_id=86497),
    )
    ol = _FakeOpenLine(send_exc=Bitrix24Error("NOT_ACTIVE_LINE", "off"))
    worker = CrmSyncWorker(repo=repo, b24sync=AsyncMock(), openline=ol)
    await worker._process_once()

    repo.mark_done.assert_awaited_once()
    repo.reschedule.assert_not_awaited()
