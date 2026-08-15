"""LoginCommandWorker — bridge-исполнитель команд онбординга TG (вариант B).

Поллер таблицы login_commands (паттерн outbox/crm_sync): подхватывает
pending-строки и исполняет Telethon-флоу:

* ``qr_login``: connect → (проверка is_user_authorized) → qr_login() →
  qr_link в строку (фронт рисует) → wait()/recreate() ≤3 итераций →
  SessionPasswordNeededError → статус password_required, поллинг пароля из
  строки (стирается при чтении) → sign_in(password) → backfill телефона/
  имени из get_me() → disconnect → ``tg_accounts.status=active`` (дальше
  аккаунт регистрирует AccountSyncWorker — единственная точка регистрации,
  столкновений на .session-файле нет: login-клиент существует только пока
  аккаунта нет в пуле).
* ``log_out``: провайдер → log_out() + force_unregister; иначе свой клиент
  → log_out(); аккаунт → offline.

Отмена: web терминализирует строку напрямую; воркер в циклах ожидания
видит чужой статус/cancel_requested и завершает. TTL-чистка: зависшие
команды → error('stale'), терминальные старше суток удаляются.
"""

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from telethon import TelegramClient
from telethon.errors import FloodWaitError, SessionPasswordNeededError

from app.bridge.session_manager import SessionManager
from app.config import get_settings
from app.db import async_session
from app.messaging.telegram.proxy import telethon_proxy
from app.models import (
    ACTIVE_STATUSES,
    LoginCommand,
    LoginCommandKind,
    LoginCommandStatus,
    TgAccount,
    TgAccountStatus,
)

logger = logging.getLogger(__name__)


class _Cancelled(Exception):
    """Команда отменена web'ом в процессе исполнения."""


def _default_client_factory(account_id: int) -> TelegramClient:
    settings = get_settings()
    session_path = f"{settings.tg_sessions_dir.rstrip('/')}/account_{account_id}/session"
    return TelegramClient(
        session_path,
        settings.tg_api_id,
        settings.tg_api_hash,
        proxy=telethon_proxy(settings),
    )


class LoginCommandWorker:
    def __init__(
        self,
        *,
        sm: SessionManager,
        account_sync,
        session_factory: async_sessionmaker[AsyncSession]
        | Callable[[], AsyncSession] = None,
        client_factory: Callable[[int], TelegramClient] | None = None,
        poll_interval: float = 2.0,
        qr_iterations: int = 3,
        control_poll_sec: float = 2.0,
        password_timeout_sec: float = 120.0,
        sweep_interval_sec: float = 300.0,
        stale_after_sec: float = 1800.0,
        terminal_ttl_sec: float = 86400.0,
    ):
        self._sm = sm
        self._account_sync = account_sync
        self._session_factory = session_factory or async_session
        self._client_factory = client_factory or _default_client_factory
        self._poll_interval = poll_interval
        self._qr_iterations = qr_iterations
        self._control_poll_sec = control_poll_sec
        self._password_timeout_sec = password_timeout_sec
        self._sweep_interval_sec = sweep_interval_sec
        self._stale_after_sec = stale_after_sec
        self._terminal_ttl_sec = terminal_ttl_sec
        self._running = False
        self._tasks: dict[int, asyncio.Task] = {}
        #: Аккаунты с живой командой: повторный /start во время waiting не
        #: должен открыть второй Telethon-клиент на том же .session-файле.
        self._account_busy: set[int] = set()

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def run(self) -> None:
        self._running = True
        await self._startup_selfheal()
        last_sweep = 0.0
        try:
            while self._running:
                try:
                    await self._poll_once()
                except Exception:  # pragma: no cover - защитная сетка
                    logger.exception("LoginCommandWorker iteration failed; continuing")
                now = asyncio.get_event_loop().time()
                if now - last_sweep >= self._sweep_interval_sec:
                    await self._sweep_once()
                    last_sweep = now
                await asyncio.sleep(self._poll_interval)
        finally:
            self._running = False

    def stop(self) -> None:
        self._running = False

    async def shutdown(self) -> None:
        """Отмена per-command тасок (в main.py — ДО sm.close_all, иначе
        login-клиенты переживают остановку пула)."""
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    async def _startup_selfheal(self) -> None:
        """Рестарт bridge посреди логина: живые команды протухли (токен QR
        истёк бы сам) — помечаем expired, менеджер начнёт заново."""
        async with self._session_factory() as s:
            await s.execute(
                update(LoginCommand)
                .where(LoginCommand.status.in_(ACTIVE_STATUSES))
                .values(status=LoginCommandStatus.expired, password_transit=None)
            )
            await s.commit()

    async def _poll_once(self) -> None:
        async with self._session_factory() as s:
            rows = (
                await s.execute(
                    select(LoginCommand)
                    .where(LoginCommand.status == LoginCommandStatus.pending)
                    .order_by(LoginCommand.id)
                    .limit(5)
                )
            ).scalars().all()
        for cmd in rows:
            task = self._tasks.get(cmd.id)
            if task is not None and not task.done():
                continue
            if cmd.account_id in self._account_busy:
                continue  # прежняя таска аккаунта ещё разбирается
            self._tasks[cmd.id] = asyncio.create_task(self._run_command(cmd.id))

    async def _sweep_once(self) -> None:
        async with self._session_factory() as s:
            await s.execute(
                update(LoginCommand)
                .where(
                    LoginCommand.status.in_(ACTIVE_STATUSES),
                    LoginCommand.created_at
                    < datetime.now(UTC) - timedelta(seconds=self._stale_after_sec),
                )
                .values(
                    status=LoginCommandStatus.error,
                    error="stale",
                    password_transit=None,
                )
            )
            await s.execute(
                delete(LoginCommand).where(
                    LoginCommand.status.notin_(ACTIVE_STATUSES),
                    LoginCommand.created_at
                    < datetime.now(UTC) - timedelta(seconds=self._terminal_ttl_sec),
                )
            )
            await s.commit()

    # ------------------------------------------------------------------ #
    # Исполнение
    # ------------------------------------------------------------------ #
    async def _run_command(self, cmd_id: int) -> None:
        account_id: int | None = None
        try:
            cmd = await self._load(cmd_id)
            if cmd is None or cmd.status is not LoginCommandStatus.pending:
                return
            account_id = cmd.account_id
            self._account_busy.add(account_id)
            if cmd.kind is LoginCommandKind.qr_login:
                await self._run_qr_login(cmd)
            else:
                await self._run_log_out(cmd)
        except Exception:
            logger.exception("LoginCommand %s упала", cmd_id)
            await self._set_status(cmd_id, LoginCommandStatus.error, "internal error")
        finally:
            self._tasks.pop(cmd_id, None)
            if account_id is not None:
                self._account_busy.discard(account_id)

    async def _run_qr_login(self, cmd: LoginCommand) -> None:
        account = await self._get_account(cmd.account_id)
        if account is None:
            await self._set_status(cmd.id, LoginCommandStatus.error, "аккаунт не найден")
            return
        if self._sm.get(account.id) is not None:
            await self._set_status(
                cmd.id,
                LoginCommandStatus.error,
                "аккаунт активен — сначала отключите его",
            )
            return

        client = self._client_factory(account.id)
        await client.connect()
        try:
            if await client.is_user_authorized():
                # bridge был вниз/сессия жива — QR не нужен. Disconnect ДО
                # active: AccountSyncWorker не должен открыть .session, пока
                # login-клиент ещё подключён.
                user = await client.get_me()
                await client.disconnect()
                await self._activate_account(account.id, user)
                await self._set_status(cmd.id, LoginCommandStatus.authorized)
                return

            qr = await client.qr_login()
            await self._set_status(
                cmd.id, LoginCommandStatus.waiting, qr_link=qr.url
            )
            for attempt in range(self._qr_iterations):
                try:
                    user = await self._wait_scan(cmd, qr)
                except _Cancelled:
                    await self._set_status(cmd.id, LoginCommandStatus.cancelled)
                    return
                except SessionPasswordNeededError:
                    await self._set_status(cmd.id, LoginCommandStatus.password_required)
                    password = await self._wait_password(cmd.id)
                    if password is None:
                        await self._set_status(
                            cmd.id, LoginCommandStatus.error, "ввод 2FA-пароля затянулся"
                        )
                        return
                    try:
                        user = await client.sign_in(password=password)
                    except Exception as exc:  # noqa: BLE001 - неверный пароль и пр.
                        await self._set_status(
                            cmd.id,
                            LoginCommandStatus.error,
                            f"неверный 2FA-пароль: {exc}"[:512],
                        )
                        return
                except TimeoutError:
                    if attempt < self._qr_iterations - 1:
                        await qr.recreate()
                        await self._set_qr(cmd.id, qr.url, attempt + 1)
                        continue
                    await self._set_status(cmd.id, LoginCommandStatus.expired)
                    return
                # Успех: disconnect ДО backfill/active — исключает гонку с
                # AccountSyncWorker на .session-файле.
                await client.disconnect()
                await self._activate_account(account.id, user)
                await self._set_status(cmd.id, LoginCommandStatus.authorized)
                return
        except FloodWaitError as e:
            await self._set_status(
                cmd.id,
                LoginCommandStatus.error,
                f"превышен лимит TG — повторите через {e.seconds}с",
            )
        except Exception as exc:
            logger.exception("TG QR-логин упал: cmd_id=%s", cmd.id)
            await self._set_status(cmd.id, LoginCommandStatus.error, str(exc)[:512])
        finally:
            try:
                await client.disconnect()
            except Exception:
                logger.debug("login client disconnect failed", exc_info=True)

    async def _wait_scan(self, cmd: LoginCommand, qr) -> object:
        """wait() обязан исполняться во время скана (Telethon); параллельно
        поллим строку — отмена web'ом/deadline не должны ждать токен.

        poll-sleep вместо wait_for: таймаут контроля неотличим от
        TimeoutError самого wait() (QR истёк) — неразличимость ломала
        ветку recreate.
        """
        wait_task = asyncio.create_task(qr.wait())
        try:
            while not wait_task.done():
                await asyncio.sleep(self._control_poll_sec)
                if await self._should_stop(cmd):
                    raise _Cancelled()
            return wait_task.result()
        finally:
            if not wait_task.done():
                wait_task.cancel()

    async def _wait_password(self, cmd_id: int) -> str | None:
        """Поллинг password_transit со стиранием при чтении; None — таймаут."""
        waited = 0.0
        while waited < self._password_timeout_sec:
            await asyncio.sleep(self._control_poll_sec)
            waited += self._control_poll_sec
            async with self._session_factory() as s:
                row = (
                    await s.execute(select(LoginCommand).where(LoginCommand.id == cmd_id))
                ).scalar_one_or_none()
                if row is None:
                    return None
                if row.status is not LoginCommandStatus.password_required:
                    return None  # отменено web'ом
                if row.password_transit:
                    # Читаем и стираем; значение — ДО update: ORM-синхронизация
                    # затирает атрибут у объекта сессии (synchronize_session).
                    password = row.password_transit
                    await s.execute(
                        update(LoginCommand)
                        .where(LoginCommand.id == cmd_id)
                        .values(password_transit=None)
                    )
                    await s.commit()
                    return password
        return None

    async def _run_log_out(self, cmd: LoginCommand) -> None:
        account = await self._get_account(cmd.account_id)
        if account is None:
            await self._set_status(cmd.id, LoginCommandStatus.done)
            return
        provider = self._sm.get(account.id)
        if provider is not None:
            try:
                await provider.log_out()
            except Exception:
                logger.warning("TG log_out провалился", exc_info=True)
            await self._account_sync.force_unregister(
                account.id, reason="admin unlink"
            )
        else:
            client = self._client_factory(account.id)
            await client.connect()
            try:
                if await client.is_user_authorized():
                    await client.log_out()
            finally:
                try:
                    await client.disconnect()
                except Exception:
                    logger.debug("logout client disconnect failed", exc_info=True)
        async with self._session_factory() as s:
            await s.execute(
                update(TgAccount)
                .where(TgAccount.id == account.id)
                .values(status=TgAccountStatus.offline)
            )
            await s.commit()
        await self._set_status(cmd.id, LoginCommandStatus.done)
        logger.info("TG аккаунт отвязан: account_id=%s", account.id)

    # ------------------------------------------------------------------ #
    # DB-хелперы
    # ------------------------------------------------------------------ #
    async def _load(self, cmd_id: int) -> LoginCommand | None:
        async with self._session_factory() as s:
            return (
                await s.execute(select(LoginCommand).where(LoginCommand.id == cmd_id))
            ).scalar_one_or_none()

    async def _get_account(self, account_id: int) -> TgAccount | None:
        async with self._session_factory() as s:
            return await s.get(TgAccount, account_id)

    async def _should_stop(self, cmd: LoginCommand) -> bool:
        row = await self._load(cmd.id)
        if row is None:
            return True
        if row.cancel_requested or row.status not in (
            LoginCommandStatus.waiting,
            LoginCommandStatus.pending,
        ):
            return True
        if row.deadline_at is not None:
            deadline = row.deadline_at
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=UTC)
            return datetime.now(UTC) > deadline
        return False

    async def _set_status(
        self, cmd_id: int, status: LoginCommandStatus, error: str | None = None,
        *, qr_link: str | None = None,
    ) -> None:
        # WHERE status IN (ACTIVE): если web уже отменил команду, воркер НЕ
        # перетирает cancelled своим error/expired (состояние не лжёт).
        async with self._session_factory() as s:
            await s.execute(
                update(LoginCommand)
                .where(
                    LoginCommand.id == cmd_id,
                    LoginCommand.status.in_(ACTIVE_STATUSES),
                )
                .values(
                    status=status, error=error, password_transit=None,
                    **({"qr_link": qr_link} if qr_link is not None else {}),
                )
            )
            await s.commit()

    async def _set_qr(self, cmd_id: int, qr_link: str, attempts: int) -> None:
        async with self._session_factory() as s:
            await s.execute(
                update(LoginCommand)
                .where(LoginCommand.id == cmd_id)
                .values(qr_link=qr_link, attempts=attempts)
            )
            await s.commit()

    async def _activate_account(self, account_id: int, user) -> None:
        """status=active + backfill телефона/имени из get_me()."""
        phone = getattr(user, "phone", None)
        name = " ".join(
            filter(None, [getattr(user, "first_name", None), getattr(user, "last_name", None)])
        ) or None
        async with self._session_factory() as s:
            await s.execute(
                update(TgAccount)
                .where(TgAccount.id == account_id)
                .values(
                    status=TgAccountStatus.active,
                    phone=phone or f"TG-acc{account_id}",
                    display_name=name,
                )
            )
            await s.commit()
