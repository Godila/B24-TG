import asyncio
import time
from collections import deque


class Throttler:
    """Throttle отправки для одного TG-аккаунта.
    Две раздельные политики: ответы (мягкая) и инициации (жёсткая, анти-бан).
    Базируется на spec §6.1 слой 1."""

    def __init__(
        self,
        reply_per_minute: int,
        init_max: int,
        init_window_sec: int,
        init_min_interval: int,
    ):
        self._reply_limit = reply_per_minute
        self._reply_window: deque[float] = deque()
        self._init_max = init_max
        self._init_window_sec = init_window_sec
        self._init_min_interval = init_min_interval
        self._init_timestamps: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self, *, is_initiation: bool) -> bool:
        async with self._lock:
            now = time.monotonic()
            if is_initiation:
                return self._check_init(now)
            return self._check_reply(now)

    def _check_reply(self, now: float) -> bool:
        cutoff = now - 60.0
        while self._reply_window and self._reply_window[0] < cutoff:
            self._reply_window.popleft()
        if len(self._reply_window) >= self._reply_limit:
            return False
        self._reply_window.append(now)
        return True

    def _check_init(self, now: float) -> bool:
        # минимальный интервал между инициациями
        if self._init_timestamps and (now - self._init_timestamps[-1]) < self._init_min_interval:
            return False
        # лимит на окно
        cutoff = now - self._init_window_sec
        while self._init_timestamps and self._init_timestamps[0] < cutoff:
            self._init_timestamps.popleft()
        if len(self._init_timestamps) >= self._init_max:
            return False
        self._init_timestamps.append(now)
        return True
