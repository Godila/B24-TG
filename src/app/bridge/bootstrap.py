"""Активация bridge-конвейера: загрузка аккаунтов, регистрация, форварды.

Модуль держит ``run_bridge()`` чистым — вся работа со стартовым потоком
аккаунтов вынесена сюда:

* ``load_active_accounts`` — запрос активных аккаунтов (всех каналов) с
  eager-load менеджера (критично: IncomingHandler читает
  ``account.manager.b24_user_id`` уже после закрытия стартовой сессии, без
  eager-load — DetachedInstanceError);
* ``register_accounts`` — подключение каждого аккаунта через SessionManager,
  устойчивое к одиночным сбоям (один упал — остальные регистрируются);
* ``forward_incoming`` — бесконечный цикл чтения ``incoming_stream`` провайдера;
* ``forward_reads`` / ``make_account_pump`` — read-квитанции (✓✓): фабрика
  pump'а объединяет оба цикла в ОДНОЙ таске на аккаунт (cancel/unregister
  гасит обе ноги сразу — teardown AccountSyncWorker не меняется).
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.models import TgAccount, TgAccountStatus

if TYPE_CHECKING:  # pragma: no cover - только для type-checker
    from app.bridge.incoming_handler import IncomingHandler
    from app.bridge.read_marker import ReadMarker
    from app.bridge.session_manager import SessionManager
    from app.messaging.provider import MessengerProvider

logger = logging.getLogger(__name__)


async def load_active_accounts(
    session_factory: async_sessionmaker[AsyncSession] | Callable[[], AsyncSession],
) -> list[TgAccount]:
    """Загрузить активные аккаунты (всех каналов) с eager-load менеджера.

    Возвращает список (возможно пустой). Менеджер загружается сразу
    (``selectinload``), чтобы после закрытия стартовой сессии обращаться к
    ``account.manager.b24_user_id`` без DetachedInstanceError.
    """
    async with session_factory() as s:
        stmt = (
            select(TgAccount)
            .where(TgAccount.status == TgAccountStatus.active)
            .options(selectinload(TgAccount.manager))
        )
        result = await s.execute(stmt)
        # list() материализует выборку до выхода из сессии (eager-load в flight).
        return list(result.scalars().all())


async def register_accounts(
    sm: "SessionManager", accounts: list[TgAccount]
) -> dict[int, TgAccount]:
    """Подключить все аккаунты через SessionManager.

    Возвращает map ``account_id -> TgAccount`` только для УСПЕШНО подключённых
    аккаунтов. Сбой одного аккаунта логируется, но не прерывает остальные —
    подписка на incoming затем идёт только по зарегистрированным.
    """
    registered: dict[int, TgAccount] = {}
    for account in accounts:
        try:
            await sm.register(account)
            registered[account.id] = account
        except Exception:
            logger.exception(
                "Failed to register account_id=%s messenger=%s phone=%s",
                account.id,
                account.messenger.value,
                account.phone,
            )
    return registered


async def forward_incoming(
    provider: "MessengerProvider",
    account: TgAccount,
    handler: "IncomingHandler",
) -> None:
    """Бесконечный цикл: incoming_stream провайдера → IncomingHandler.

    Ошибка обработки одного сообщения логируется, но не рвёт подписку —
    цикл продолжается для следующих сообщений.
    """
    stream = provider.incoming_stream()
    async for msg in stream:
        try:
            await handler.handle(msg, account=account)
        except Exception:
            logger.exception("handler failed for msg from sender=%s", msg.sender_external_id)


async def forward_reads(
    provider: "MessengerProvider",
    account: TgAccount,
    read_marker: "ReadMarker",
) -> None:
    """Бесконечный цикл: read_receipt_stream провайдера → ReadMarker.

    Ошибка одной квитанции логируется, но не рвёт подписку (как у incoming).
    """
    async for receipt in provider.read_receipt_stream():
        try:
            await read_marker.apply(receipt, account=account)
        except Exception:
            logger.exception("read marker failed for chat=%s", receipt.external_chat_id)


def make_account_pump(read_marker: "ReadMarker") -> Callable[..., Awaitable[None]]:
    """Фабрика forward-колбэка AccountSyncWorker: одна таска на аккаунт,
    внутри gather обеих ног (incoming + read).

    Сигнатура полученного pump(provider, account, handler) совместима с
    ForwardFn; cancel таски (unregister/shutdown) гасит обе ноги, MAX
    завершает обе сентинелами при disconnect — «осиротевшая» нога у
    gather исключена (тела циклов не бросают исключений по построению).
    """

    async def pump(
        provider: "MessengerProvider",
        account: TgAccount,
        handler: "IncomingHandler",
    ) -> None:
        await asyncio.gather(
            forward_incoming(provider, account, handler),
            forward_reads(provider, account, read_marker),
        )

    return pump
