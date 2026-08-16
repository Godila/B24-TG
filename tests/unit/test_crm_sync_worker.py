"""CrmSyncWorker: mock-repo + mock-sync — успех/провал/backoff/терминал."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from app.b24.sync import SyncResult
from app.bridge.crm_sync_worker import CrmSyncData, CrmSyncWorker
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
        "crm_deal_id": 100,
        "crm_entity_type": "deal",
        "assigned_b24_user_id": 15,
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
            contact_id=42, deal_id=100, is_new=True, timeline_comment_id=999,
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
        11, contact_id=42, deal_id=100, timeline_comment_id=999,
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
async def test_inbound_no_assigned_manager_is_terminal():
    repo = _make_repo([_make_item()], _make_data(assigned_b24_user_id=None))
    sync = AsyncMock()

    worker = CrmSyncWorker(repo=repo, b24sync=sync, max_attempts=5)
    await worker._process_once()

    sync.process_inbound.assert_not_awaited()
    repo.mark_failed.assert_awaited_once()
    assert repo.mark_failed.call_args.args[1] == "no_assigned_manager"


# ---------------------------------------------------------------------- #
# kind=outbound
# ---------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_outbound_success_sets_comment_and_marks_done():
    repo = _make_repo(
        [_make_item(kind=KIND_OUTBOUND)], _make_data(message_text="Ответ менеджера")
    )
    sync = AsyncMock()
    sync.process_outbound = AsyncMock(return_value=555)

    worker = CrmSyncWorker(repo=repo, b24sync=sync, max_attempts=5)
    await worker._process_once()

    sync.process_outbound.assert_awaited_once_with(
        dialog_deal_id=100, dialog_entity_type="deal",
        contact_id=42, text="Ответ менеджера", timeline_mode="all",
    )
    repo.set_timeline_comment.assert_awaited_once_with(11, 555)
    repo.mark_done.assert_awaited_once()


@pytest.mark.asyncio
async def test_outbound_without_entity_done_without_comment():
    """Ни сделки, ни контакта — писать некуда: done без timeline_comment_id
    (для исходящих это не ошибка)."""
    repo = _make_repo(
        [_make_item(kind=KIND_OUTBOUND)],
        _make_data(crm_deal_id=None, crm_entity_type=None, crm_contact_id=None),
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
        return_value=SyncResult(contact_id=42, deal_id=100, is_new=True)
    )

    worker = CrmSyncWorker(repo=repo, b24sync=sync, max_attempts=5)
    await worker._process_once()

    assert (
        sync.process_inbound.call_args.kwargs.get("timeline_mode") == "first"
    )
