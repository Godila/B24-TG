"""Медиа-ветка OutboxWorker: send_media, терминальные attachment-гейты."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.bridge.outbox_worker import OutboxWorker
from app.media.storage import MediaStorage
from app.messaging.types import SendResult
from app.models import AttachmentType, OutboxItem, OutboxStatus


def _make_item(**kw) -> OutboxItem:
    defaults = {
        "id": 1,
        "dialog_id": 10,
        "tg_account_id": 7,
        "text": "capt",
        "status": OutboxStatus.queued,
        "attempts": 0,
        "next_attempt_at": datetime.now(UTC),
        "last_error": None,
        "is_initiation": False,
        "external_chat_id": "12345",
        "attachment_id": None,
    }
    defaults.update(kw)
    return OutboxItem(**defaults)


def _attachment(rel_path, type_=AttachmentType.photo):
    return SimpleNamespace(
        file_path=rel_path,
        type=type_,
        mime_type="image/jpeg",
        file_name="x.jpg",
    )


def _make_worker(tmp_path, item, provider, repo=None):
    repo = repo or AsyncMock()
    repo.fetch_due = AsyncMock(return_value=[item])
    throttler = AsyncMock()
    throttler.acquire = AsyncMock(return_value=True)
    worker = OutboxWorker(
        repo=repo,
        get_provider=lambda aid: provider,
        throttler_factory=lambda aid: throttler,
        max_attempts=5,
        media_storage=MediaStorage(tmp_path),
    )
    return worker, repo


@pytest.mark.asyncio
async def test_attachment_item_uses_send_media(tmp_path):
    rel = "out/abc.jpg"
    (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / rel).write_bytes(b"jpg")

    item = _make_item(attachment_id=3, text="capt")
    item.attachment = _attachment(rel)

    provider = AsyncMock()
    provider.supports_media = MagicMock(return_value=True)
    provider.send_media = AsyncMock(return_value=SendResult(success=True, external_message_id="77"))

    worker, repo = _make_worker(tmp_path, item, provider)
    await worker._process_once()

    provider.send_media.assert_awaited_once()
    args, kwargs = provider.send_media.await_args
    assert args[0] == "12345"
    assert str(args[1]) == str((tmp_path / rel).resolve())
    assert args[2] is AttachmentType.photo
    assert kwargs["caption"] == "capt"
    provider.send_message.assert_not_awaited()
    repo.mark_sent.assert_awaited_once()


@pytest.mark.asyncio
async def test_attachment_missing_file_fails_terminal(tmp_path):
    """Строки нет/файла нет — терминальный failed без расхода попыток
    (файл не вернуть ретраями)."""
    item = _make_item(attachment_id=3)
    item.attachment = _attachment("out/gone.jpg")  # файла на диске нет

    provider = AsyncMock()
    provider.supports_media = MagicMock(return_value=True)

    worker, repo = _make_worker(tmp_path, item, provider)
    await worker._process_once()

    provider.send_media.assert_not_awaited()
    repo.mark_failed.assert_awaited_once()
    assert repo.mark_failed.call_args.args[1] == "attachment_missing"
    repo.reschedule.assert_not_awaited()


@pytest.mark.asyncio
async def test_attachment_without_row_fails_terminal(tmp_path):
    """attachment_id проставлен, строки attachment нет (битая ссылка)."""
    item = _make_item(attachment_id=3)
    item.attachment = None

    provider = AsyncMock()
    worker, repo = _make_worker(tmp_path, item, provider)
    await worker._process_once()

    repo.mark_failed.assert_awaited_once()
    assert repo.mark_failed.call_args.args[1] == "attachment_missing"


@pytest.mark.asyncio
async def test_attachment_unsupported_channel_fails_terminal(tmp_path):
    rel = "out/abc.pdf"
    (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / rel).write_bytes(b"pdf")

    item = _make_item(attachment_id=3)
    item.attachment = _attachment(rel, type_=AttachmentType.file)

    provider = AsyncMock()
    provider.supports_media = MagicMock(return_value=False)  # канал без медиа

    worker, repo = _make_worker(tmp_path, item, provider)
    await worker._process_once()

    provider.send_media.assert_not_awaited()
    repo.mark_failed.assert_awaited_once()
    assert repo.mark_failed.call_args.args[1] == "media_not_supported"


@pytest.mark.asyncio
async def test_attachment_without_storage_fails_terminal(tmp_path):
    """Воркер без MediaStorage (старый wiring) не отправляет медиа-элементы."""
    item = _make_item(attachment_id=3)
    item.attachment = _attachment("out/x.jpg")

    provider = AsyncMock()
    repo = AsyncMock()
    repo.fetch_due = AsyncMock(return_value=[item])
    throttler = AsyncMock()
    throttler.acquire = AsyncMock(return_value=True)
    worker = OutboxWorker(
        repo=repo,
        get_provider=lambda aid: provider,
        throttler_factory=lambda aid: throttler,
        max_attempts=5,
        media_storage=None,
    )
    await worker._process_once()

    repo.mark_failed.assert_awaited_once()
    assert repo.mark_failed.call_args.args[1] == "attachment_missing"


@pytest.mark.asyncio
async def test_text_item_unaffected(tmp_path):
    """attachment_id=None — прежний путь send_message (регресс-страховка)."""
    item = _make_item(attachment_id=None, text="привет")
    item.attachment = None

    provider = AsyncMock()
    provider.send_message = AsyncMock(
        return_value=SendResult(success=True, external_message_id="5")
    )

    worker, repo = _make_worker(tmp_path, item, provider)
    await worker._process_once()

    provider.send_message.assert_awaited_once()
    provider.send_media.assert_not_awaited()
    repo.mark_sent.assert_awaited_once()
