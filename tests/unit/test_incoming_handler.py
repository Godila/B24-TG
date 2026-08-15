from unittest.mock import AsyncMock, MagicMock

import pytest

from app.bridge.incoming_handler import IncomingHandler
from app.messaging.types import ContentType, IncomingMessage
from app.models import Message, Messenger


def _make_msg(**kw):
    defaults = {
        "messenger": Messenger.tg,
        "external_chat_id": "12345",
        "sender_external_id": "999",
        "sender_name": "Иван",
        "sender_phone": "+79991234567",
        "sender_username": None,
        "content_type": ContentType.text,
        "text": "Привет",
        "external_message_id": "1",
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
    ``async with ... as session``.

    ``flush`` эмулирует присвоение PK: без него Message.id остаётся None и
    handler не поставит crm_sync-задачу (message_id неизвестен).
    """
    session = AsyncMock()
    session.__aenter__.return_value = session
    session.execute.return_value = MagicMock(scalar_one_or_none=lambda: None)

    def _assign_message_pk():
        for call in session.add.call_args_list:
            obj = call.args[0] if call.args else None
            if isinstance(obj, Message) and obj.id is None:
                obj.id = 42

    session.flush = AsyncMock(side_effect=_assign_message_pk)
    return session


@pytest.mark.asyncio
async def test_handle_persists_then_enqueues_crm_sync():
    """Порядок план 006: persist (без CRM-полей) → crm_sync enqueue."""
    account = MagicMock()
    account.id = 7
    account.manager_id = 3
    account.manager.b24_user_id = 15

    enqueue = AsyncMock()
    db_session = _make_db_session()

    handler = IncomingHandler(
        crm_sync_enqueue=enqueue,
        db_session_factory=lambda: db_session,
    )
    await handler.handle(_make_msg(), account=account)

    # persist: session использована как контекст-менеджер и закоммичена
    db_session.__aenter__.assert_awaited()
    db_session.commit.assert_awaited()
    # что-то добавлено (Contact, Dialog, Message)
    assert db_session.add.call_count >= 3
    # CRM — через очередь, с id сохранённого сообщения
    enqueue.assert_awaited_once()
    kwargs = enqueue.call_args.kwargs
    assert kwargs["kind"] == "inbound"
    assert kwargs["message_id"] == 42


@pytest.mark.asyncio
async def test_handle_survives_enqueue_failure():
    """Сбой постановки в crm_sync не должен терять уже сохранённое сообщение
    (и не должен ронять обработку)."""
    account = MagicMock()
    account.id = 7
    account.manager_id = 3
    account.manager.b24_user_id = 15

    enqueue = AsyncMock(side_effect=RuntimeError("db down"))
    db_session = _make_db_session()

    handler = IncomingHandler(
        crm_sync_enqueue=enqueue,
        db_session_factory=lambda: db_session,
    )
    # Не бросает — сообщение уже в нашей БД.
    await handler.handle(_make_msg(), account=account)

    db_session.commit.assert_awaited()
    enqueue.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_skips_duplicate_message():
    """Идемпотентность: если сообщение (dialog+external_message_id) уже сохранено,
    повторная обработка не создаёт дубль Message И не ставит вторую
    CRM-задачу."""
    account = MagicMock()
    account.id = 7
    account.manager_id = 3
    account.manager.b24_user_id = 15

    enqueue = AsyncMock()

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
        crm_sync_enqueue=enqueue,
        db_session_factory=lambda: session,
    )
    await handler.handle(_make_msg(), account=account)

    # Контакт и Диалог добавлены (2 add), но Message НЕ добавлен (дубль).
    assert session.add.call_count == 2
    session.commit.assert_awaited()
    enqueue.assert_not_awaited()
