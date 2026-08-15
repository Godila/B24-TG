"""AccountSyncWorker — синхронизация active-аккаунтов ↔ пула провайдеров.

Решает главную боль онбординга: после QR-подключения (/admin/max пишет
токен в БД и ставит status=active) bridge подхватывает аккаунт сам, без
рестарта. Периодический опрос БД:

  * новые active-аккаунты без провайдера → register + forward_incoming;
  * пропавшие из active → unregister, НО только «мёртвых»: провайдер в
    offline-статусе с живым реконнект-циклом не трогаем (иначе убили бы
    его самолечение);
  * сбой регистрации (например, MaxAuthError — токен отозван) →
    on_register_failure: аккаунт переводится в offline (выпадает из
    active-сета — нет молотилки LOGIN каждые N секунд) + алерт админу.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bridge.bootstrap import forward_incoming, load_active_accounts
from app.bridge.session_manager import SessionManager
from app.messaging.max.protocol import MaxAuthError
from app.models import Messenger, TgAccount, TgAccountStatus

logger = logging.getLogger(__name__)

ForwardFn = Callable[..., Awaitable[None]]
RegisterFailureHook = Callable[[TgAccount, Exception], Awaitable[None]]


class AccountSyncWorker:
    def __init__(
        self,
        *,
        sm: SessionManager,
        handler,
        session_factory: async_sessionmaker[AsyncSession] | Callable[[], AsyncSession],
        forward: ForwardFn = forward_incoming,
        interval_sec: float = 20.0,
        on_register_failure: RegisterFailureHook | None = None,
    ):
        self._sm = sm
        self._handler = handler
        self._session_factory = session_factory
        self._forward = forward
        self._interval_sec = interval_sec
        self._on_register_failure = on_register_failure
        self._running = False
        self._forward_tasks: dict[int, asyncio.Task] = {}

    async def run(self) -> None:
        self._running = True
        try:
            while self._running:
                try:
                    await self._sync_once()
                except Exception:  # pragma: no cover - защитная сетка
                    logger.exception("AccountSyncWorker iteration failed; continuing")
                await asyncio.sleep(self._interval_sec)
        finally:
            self._running = False

    def stop(self) -> None:
        self._running = False

    async def cancel_forwards(self) -> None:
        """Отменить forward-таски (в shutdown — ДО sm.close_all)."""
        for task in self._forward_tasks.values():
            task.cancel()
        await asyncio.gather(*self._forward_tasks.values(), return_exceptions=True)
        self._forward_tasks.clear()

    async def _sync_once(self) -> None:
        active = await load_active_accounts(self._session_factory)
        active_ids = {a.id for a in active}
        active_by_id = {a.id: a for a in active}

        # 1. Новые: active в БД, но нет провайдера.
        for account_id in sorted(active_ids - self._sm.registered_ids()):
            account = active_by_id[account_id]
            try:
                await self._sm.register(account)
            except Exception as exc:
                logger.exception(
                    "AccountSync: register failed account_id=%s messenger=%s",
                    account.id, account.messenger.value,
                )
                if self._on_register_failure is not None:
                    try:
                        await self._on_register_failure(account, exc)
                    except Exception:
                        logger.exception("on_register_failure hook failed")
                continue
            provider = self._sm.get(account_id)
            if provider is not None:
                self._start_forward(account_id, provider, account)

        # 2. Лишние/устаревшие: провайдер есть, но аккаунт выпал из active,
        #    умер (токен отозван) или перепривязан новым QR (токен в БД
        #    отличается от работающего — провайдер продолжал бы на старом).
        for account_id in sorted(self._sm.registered_ids() & active_ids):
            provider = self._sm.get(account_id)
            account = active_by_id[account_id]
            if provider is None or account is None:
                continue
            if provider.credential_token() != account.token:
                await self._unregister(account_id, reason="credentials rotated")
                continue
            if provider.is_dead():
                await self._unregister(account_id, reason="provider dead")

        for account_id in sorted(self._sm.registered_ids() - active_ids):
            provider = self._sm.get(account_id)
            if provider is None:
                continue
            if provider.is_dead():
                await self._unregister(account_id, reason="provider dead")
                continue
            # MAX, отвязанный админом (token=NULL): провайдер продолжал бы
            # работать на старом токене — снимаем.
            if await self._is_deactivated(account_id):
                await self._unregister(account_id, reason="deactivated by admin")
                continue
            # offline-статус при живом провайдере: HealthChecker видит
            # разрыв, реконнект-цикл продолжается — не мешаем.
            logger.info(
                "AccountSync: account_id=%s offline, провайдер жив — не трогаем",
                account_id,
            )

    async def _is_deactivated(self, account_id: int) -> bool:
        async with self._session_factory() as s:
            account = (
                await s.execute(
                    select(TgAccount).where(TgAccount.id == account_id)
                )
            ).scalar_one_or_none()
        return (
            account is not None
            and account.token is None
            and account.messenger == Messenger.max
        )

    def _start_forward(self, account_id: int, provider, account) -> None:
        if account_id in self._forward_tasks and not self._forward_tasks[account_id].done():
            return
        self._forward_tasks[account_id] = asyncio.create_task(
            self._forward(provider, account, self._handler)
        )
        logger.info("AccountSync: forward запущен account_id=%s", account_id)

    async def _unregister(self, account_id: int, *, reason: str) -> None:
        task = self._forward_tasks.pop(account_id, None)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self._sm.unregister(account_id)
        logger.info("AccountSync: unregister account_id=%s (%s)", account_id, reason)

    async def force_unregister(self, account_id: int, *, reason: str) -> None:
        """Публичная обёртка _unregister: гасит forward-таску и провайдера.

        Нужна LoginCommandWorker'у после TG log_out (админская отвязка)."""
        await self._unregister(account_id, reason=reason)


def make_register_failure_hook(
    session_factory, notifier, admin_user_id: int
) -> RegisterFailureHook:
    """Хук для on_register_failure: status=offline + алерт админу.

    Аккаунт выпадает из active-сета (нет молотилки LOGIN мёртвым токеном),
    админ получает алерт, менеджер переподключается через /admin/max.
    """

    async def hook(account: TgAccount, exc: Exception) -> None:
        # Терминально — только отозванный токен (ретраи бессмысленны и
        # выжигают LOGIN-бюджет). Транзиентные сбои (сеть/таймаут) аккаунт
        # в active оставляют — следующий тик воркера попробует снова.
        terminal = isinstance(exc, MaxAuthError)
        if terminal:
            async with session_factory() as s:
                await s.execute(
                    update(TgAccount)
                    .where(TgAccount.id == account.id)
                    .values(status=TgAccountStatus.offline)
                )
                await s.commit()
        if notifier is not None:
            try:
                await notifier(
                    admin_user_id,
                    f"⚠️ Bitrix-TG: {account.messenger.value.upper()}-аккаунт "
                    f"id={account.id} ({account.phone}) не подключается"
                    + (" — сессия отозвана, переподключите (для MAX — /admin/max)"
                       if terminal else " (сетевой сбой, повторим автоматически)")
                    + f": {exc}",
                )
            except Exception:
                logger.exception("alert о неудачной регистрации не доставлен")

    return hook
