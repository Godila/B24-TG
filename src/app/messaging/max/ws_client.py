"""MaxWsClient — транспорт WS-протокола MAX (одно соединение).

Порт спайка scripts/spike_max_login.py (MaxWsClient), production-качество:
типизированные ошибки cmd=3, callback на push, seam для тестов (connect_fn).

Клиент НЕ знает про реконнект: одно соединение живёт, пока живо; обрыв
фэйлит pending-запросы ConnectionError'ом, а владелец (провайдер/QR-флоу)
решает, что делать дальше. Серверный heartbeat (push op=1) автоотвечается
здесь — это протокольная обязанность любого соединения.
"""

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable

from app.messaging.max.protocol import OP_PING, classify_error

logger = logging.getLogger(__name__)

PushCallback = Callable[[dict], Awaitable[None]]


class MaxWsClient:
    """Одно WS-соединение: seq-матчинг запросов + диспетчер push'ей."""

    def __init__(
        self,
        *,
        url: str,
        headers: dict[str, str],
        request_timeout: float = 15.0,
        connect_fn: Callable[[str, dict[str, str]], Awaitable[object]] | None = None,
    ):
        self._url = url
        self._headers = headers
        self._request_timeout = request_timeout
        self._connect_fn = connect_fn
        self._ws = None
        self._seq = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._reader: asyncio.Task | None = None
        self._open = False
        self._on_push: PushCallback | None = None
        #: monotonic-время последней отправки (для idle-heartbeat владельца)
        self.last_send = time.monotonic()

    # ------------------------------------------------------------------ #
    # Жизненный цикл
    # ------------------------------------------------------------------ #
    @property
    def closed(self) -> bool:
        return not self._open

    def is_connected(self) -> bool:
        return self._open

    def on_push(self, cb: PushCallback) -> None:
        self._on_push = cb

    async def connect(self) -> None:
        if self._connect_fn is not None:
            self._ws = await self._connect_fn(self._url, self._headers)
        else:
            self._ws = await self._connect_websockets(self._url, self._headers)
        self._open = True
        self._reader = asyncio.create_task(self._read_loop())

    @staticmethod
    async def _connect_websockets(url: str, headers: dict[str, str]):
        import websockets

        # websockets>=12: additional_headers; старые версии: extra_headers.
        try:
            return await websockets.connect(url, additional_headers=headers)
        except TypeError:
            return await websockets.connect(url, extra_headers=headers)

    async def close(self) -> None:
        self._open = False
        if self._reader:
            self._reader.cancel()
            self._reader = None
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # best-effort: соединение могло уже умереть
                logger.debug("close() best-effort failed", exc_info=True)
            self._ws = None

    # ------------------------------------------------------------------ #
    # Запросы (seq-матчинг)
    # ------------------------------------------------------------------ #
    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    async def _send_raw(self, frame: dict) -> None:
        await self._ws.send(json.dumps(frame, ensure_ascii=False))
        self.last_send = time.monotonic()

    async def request(self, opcode: int, payload: dict | None = None,
                      *, timeout: float | None = None) -> dict:
        """Отправить запрос и дождаться ответа с совпадающим seq.

        cmd=3 → типизированная ошибка classify_error; таймаут → TimeoutError;
        разрыв соединения → ConnectionError.
        """
        seq = self._next_seq()
        frame = {"ver": 11, "cmd": 0, "seq": seq, "opcode": opcode, "payload": payload or {}}
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[seq] = fut
        try:
            await self._send_raw(frame)
            resp = await asyncio.wait_for(fut, timeout or self._request_timeout)
        finally:
            self._pending.pop(seq, None)
        if resp.get("cmd") == 3:
            raise classify_error(opcode, resp.get("payload") or {})
        return resp

    # ------------------------------------------------------------------ #
    # Чтение
    # ------------------------------------------------------------------ #
    async def _read_loop(self) -> None:
        try:
            async for raw in self._ws:
                frame = json.loads(raw)
                cmd = frame.get("cmd")
                seq = frame.get("seq")
                if cmd in (1, 3) and seq in self._pending:
                    fut = self._pending.pop(seq)
                    if not fut.done():
                        fut.set_result(frame)
                elif cmd in (1, 3):
                    logger.warning(
                        "MAX frame cmd=%s с неизвестным seq=%s (op=%s)",
                        cmd, seq, frame.get("opcode"),
                    )
                else:
                    await self._handle_push(frame)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.info("MAX WS соединение закрыто: %s", exc_info=True)
        finally:
            self._open = False
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("max ws closed"))
            self._pending.clear()

    async def _handle_push(self, frame: dict) -> None:
        op = frame.get("opcode")
        if op == OP_PING:
            # Серверный heartbeat: отвечаем interactive-pong'ом (протокол
            # требует активности; простой 30-60с = разрыв).
            await self._send_raw(
                {
                    "ver": 11,
                    "cmd": 0,
                    "seq": self._next_seq(),
                    "opcode": OP_PING,
                    "payload": {"interactive": True},
                }
            )
            return
        if self._on_push is not None:
            try:
                await self._on_push(frame)
            except Exception:
                logger.exception("MAX push callback упал (frame op=%s)", op)
        else:
            snippet = json.dumps(frame.get("payload") or {}, ensure_ascii=False)[:160]
            logger.info("MAX push op=%s: %s", op, snippet)
