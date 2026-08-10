from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from app.bridge.outbox_worker import OutboxWorker
from app.models import OutboxItem, OutboxStatus
from app.messaging.types import SendResult


def _make_item(**kw) -> OutboxItem:
    defaults = dict(
        id=1, dialog_id=10, tg_account_id=7, text="hi",
        status=OutboxStatus.queued, attempts=0,
        next_attempt_at=datetime.now(timezone.utc),
        last_error=None, is_initiation=False, external_chat_id="12345",
    )
    defaults.update(kw)
    return OutboxItem(**defaults)


@pytest.mark.asyncio
async def test_process_success_marks_sent():
    repo = AsyncMock()
    repo.fetch_due = AsyncMock(return_value=[_make_item()])
    repo.mark_sent = AsyncMock()

    provider = AsyncMock()
    provider.send_message = AsyncMock(return_value=SendResult(success=True, external_message_id=55))

    throttler = AsyncMock()
    throttler.acquire = AsyncMock(return_value=True)

    worker = OutboxWorker(repo=repo, get_provider=lambda aid: provider,
                          throttler_factory=lambda aid: throttler,
                          max_attempts=5)
    await worker._process_once()

    repo.mark_sent.assert_awaited_once()
    # mark_sent(item, external_message_id) — external_message_id это второй позиционный аргумент.
    assert repo.mark_sent.call_args.args[1] == 55


@pytest.mark.asyncio
async def test_process_floodwait_reschedules():
    repo = AsyncMock()
    item = _make_item()
    repo.fetch_due = AsyncMock(return_value=[item])
    repo.reschedule = AsyncMock()

    provider = AsyncMock()
    provider.send_message = AsyncMock(
        return_value=SendResult(success=False, flood_wait_seconds=120, error="flood_wait")
    )

    throttler = AsyncMock()
    throttler.acquire = AsyncMock(return_value=True)

    worker = OutboxWorker(repo=repo, get_provider=lambda aid: provider,
                          throttler_factory=lambda aid: throttler, max_attempts=5)
    await worker._process_once()

    repo.reschedule.assert_awaited_once()
    # next_attempt_at сдвинут на ~120 сек
    _, kwargs = repo.reschedule.call_args
    assert kwargs["delay_seconds"] == 120


@pytest.mark.asyncio
async def test_process_max_attempts_marks_failed():
    repo = AsyncMock()
    item = _make_item(attempts=5)
    repo.fetch_due = AsyncMock(return_value=[item])
    repo.mark_failed = AsyncMock()

    provider = AsyncMock()
    provider.send_message = AsyncMock(return_value=SendResult(success=False, error="boom"))

    throttler = AsyncMock()
    throttler.acquire = AsyncMock(return_value=True)

    worker = OutboxWorker(repo=repo, get_provider=lambda aid: provider,
                          throttler_factory=lambda aid: throttler, max_attempts=5)
    await worker._process_once()

    repo.mark_failed.assert_awaited_once()


@pytest.mark.asyncio
async def test_throttle_rejects_reschedules_short():
    repo = AsyncMock()
    item = _make_item()
    repo.fetch_due = AsyncMock(return_value=[item])
    repo.reschedule = AsyncMock()

    provider = AsyncMock()
    throttler = AsyncMock()
    throttler.acquire = AsyncMock(return_value=False)  # лимит исчерпан

    worker = OutboxWorker(repo=repo, get_provider=lambda aid: provider,
                          throttler_factory=lambda aid: throttler, max_attempts=5)
    await worker._process_once()

    provider.send_message.assert_not_awaited()
    repo.reschedule.assert_awaited_once()
    _, kwargs = repo.reschedule.call_args
    assert kwargs["delay_seconds"] < 120


@pytest.mark.asyncio
async def test_no_provider_reschedules():
    repo = AsyncMock()
    item = _make_item(tg_account_id=999)
    repo.fetch_due = AsyncMock(return_value=[item])
    repo.reschedule = AsyncMock()

    throttler = AsyncMock()

    worker = OutboxWorker(
        repo=repo,
        get_provider=lambda aid: None,  # нет провайдера для этого аккаунта
        throttler_factory=lambda aid: throttler,
        max_attempts=5,
    )
    await worker._process_once()

    repo.reschedule.assert_awaited_once()
    _, kwargs = repo.reschedule.call_args
    assert kwargs["delay_seconds"] == 30
    assert kwargs["error"] == "no_provider"


@pytest.mark.asyncio
async def test_generic_failure_exponential_backoff():
    repo = AsyncMock()
    item = _make_item(attempts=2)  # 3-я попытка (0-indexed), backoff = 30 * 2^2 = 120
    repo.fetch_due = AsyncMock(return_value=[item])
    repo.reschedule = AsyncMock()

    provider = AsyncMock()
    provider.send_message = AsyncMock(return_value=SendResult(success=False, error="network"))

    throttler = AsyncMock()
    throttler.acquire = AsyncMock(return_value=True)

    worker = OutboxWorker(
        repo=repo,
        get_provider=lambda aid: provider,
        throttler_factory=lambda aid: throttler,
        max_attempts=5,
    )
    await worker._process_once()

    repo.reschedule.assert_awaited_once()
    _, kwargs = repo.reschedule.call_args
    assert kwargs["delay_seconds"] == 120  # 30 * 2^2
    assert kwargs["error"] == "network"
