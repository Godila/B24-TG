from unittest.mock import AsyncMock, MagicMock

import pytest

from app.b24.sync import SyncResult
from app.bridge.incoming_handler import IncomingHandler
from app.messaging.types import ContentType, IncomingMessage


def _make_msg(**kw):
    defaults = {
        "account_id": 7,
        "external_chat_id": "12345",
        "sender_tg_id": 999,
        "sender_name": "Иван",
        "sender_phone": "+79991234567",
        "sender_username": None,
        "content_type": ContentType.text,
        "text": "Привет",
        "external_message_id": 1,
        "is_reply": False,
    }
    defaults.update(kw)
    return IncomingMessage(**defaults)


def _make_db_session() -> AsyncMock:
    """AsyncMock сессии, где select-запросы не находят записей.

    ``execute`` — awaitable (как в реальной AsyncSession), но возвращает
    синхронный Result-подобный объект, чей ``scalar_one_or_none()`` даёт None
    (как в SQLAlchemy). Без этого AsyncMock вернёт coroutine, и upsert уйдёт
    в ветку «update» вместо «create», не вызвав ``add()`` трижды.

    ``__aenter__`` возвращает саму сессию (self-yield), чтобы проверки
    ``add``/``commit`` на ``db_session`` видели те же вызовы, что и код внутри
    ``async with ... as session`` (по умолчанию AsyncMock возвращает дочерний
    mock, и настройки ``execute`` терялись бы).
    """
    session = AsyncMock()
    session.__aenter__.return_value = session
    session.execute.return_value = MagicMock(scalar_one_or_none=lambda: None)
    return session


@pytest.mark.asyncio
async def test_handle_calls_sync_with_manager_b24_user_id():
    account = MagicMock()
    account.id = 7
    account.manager.b24_user_id = 15

    b24sync = AsyncMock()
    b24sync.process_inbound = AsyncMock(
        return_value=SyncResult(
            contact_id=42, deal_id=100, is_new=False, timeline_comment_id=999,
        )
    )

    db_session = _make_db_session()

    handler = IncomingHandler(
        session_mgr=MagicMock(),
        b24sync=b24sync,
        db_session_factory=lambda: db_session,
    )
    await handler.handle(_make_msg(), account=account)

    b24sync.process_inbound.assert_awaited_once()
    call_kwargs = b24sync.process_inbound.call_args.kwargs
    assert call_kwargs["sender_name"] == "Иван"
    assert call_kwargs["sender_phone"] == "+79991234567"
    assert call_kwargs["assigned_b24_user_id"] == 15


@pytest.mark.asyncio
async def test_handle_persists_message_to_db():
    account = MagicMock()
    account.id = 7
    account.manager.b24_user_id = 15

    b24sync = AsyncMock()
    b24sync.process_inbound = AsyncMock(
        return_value=SyncResult(
            contact_id=42, deal_id=100, is_new=False, timeline_comment_id=999,
        )
    )

    db_session = _make_db_session()

    handler = IncomingHandler(
        session_mgr=MagicMock(),
        b24sync=b24sync,
        db_session_factory=lambda: db_session,
    )
    await handler.handle(_make_msg(), account=account)

    # session was used as a context manager and committed
    db_session.__aenter__.assert_awaited()
    db_session.commit.assert_awaited()
    # something was added (Contact, Dialog, Message)
    assert db_session.add.call_count >= 3


@pytest.mark.asyncio
async def test_handle_survives_sync_failure():
    account = MagicMock()
    account.id = 7
    account.manager.b24_user_id = 15

    b24sync = AsyncMock()
    b24sync.process_inbound = AsyncMock(side_effect=RuntimeError("b24 down"))

    db_session = _make_db_session()

    handler = IncomingHandler(
        session_mgr=MagicMock(),
        b24sync=b24sync,
        db_session_factory=lambda: db_session,
    )
    # Must NOT raise — message still persisted even if CRM sync fails.
    await handler.handle(_make_msg(), account=account)

    db_session.commit.assert_awaited()
    assert db_session.add.call_count >= 3


@pytest.mark.asyncio
async def test_handle_skips_duplicate_message():
    """Идемпотентность: если сообщение (dialog+tg_message_id) уже сохранено,
    повторная обработка не создаёт дубль Message."""
    account = MagicMock()
    account.id = 7
    account.manager.b24_user_id = 15

    b24sync = AsyncMock()
    b24sync.process_inbound = AsyncMock(
        return_value=SyncResult(
            contact_id=42, deal_id=100, is_new=False, timeline_comment_id=999,
        )
    )

    session = AsyncMock()
    session.__aenter__.return_value = session
    # execute вызовы по порядку:
    # 1) Contact — None (нет → создаст), 2) Dialog — None (нет → создаст),
    # 3) Message — найден (дубль → пропустим).
    none_result = MagicMock(scalar_one_or_none=lambda: None)
    found_msg = MagicMock()
    found_result = MagicMock(scalar_one_or_none=lambda: found_msg)
    session.execute.side_effect = [none_result, none_result, found_result]

    handler = IncomingHandler(
        session_mgr=MagicMock(),
        b24sync=b24sync,
        db_session_factory=lambda: session,
    )
    await handler.handle(_make_msg(), account=account)

    # Контакт и Диалог добавлены (2 add), но Message НЕ добавлен (дубль).
    assert session.add.call_count == 2
    session.commit.assert_awaited()
