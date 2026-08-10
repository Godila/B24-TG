import pytest

from app.bridge.throttler import Throttler


@pytest.mark.asyncio
async def test_reply_always_allowed_under_limit():
    t = Throttler(reply_per_minute=20, init_max=10, init_window_sec=180, init_min_interval=5)
    for _ in range(20):
        allowed = await t.acquire(is_initiation=False)
        assert allowed is True


@pytest.mark.asyncio
async def test_reply_blocked_over_limit():
    t = Throttler(reply_per_minute=2, init_max=10, init_window_sec=180, init_min_interval=5)
    assert await t.acquire(is_initiation=False) is True
    assert await t.acquire(is_initiation=False) is True
    assert await t.acquire(is_initiation=False) is False  # лимит исчерпан


@pytest.mark.asyncio
async def test_init_respects_min_interval(monkeypatch):
    # Имитируем время без ожидания
    import app.bridge.throttler as mod

    fake_time = [1000.0]
    monkeypatch.setattr(mod.time, "monotonic", lambda: fake_time[0])

    t = Throttler(reply_per_minute=20, init_max=10, init_window_sec=180, init_min_interval=5)
    assert await t.acquire(is_initiation=True) is True

    fake_time[0] = 1002.0  # прошло 2 сек < 5 сек
    assert await t.acquire(is_initiation=True) is False

    fake_time[0] = 1006.0  # прошло 6 сек >= 5 сек
    assert await t.acquire(is_initiation=True) is True


@pytest.mark.asyncio
async def test_init_respects_window_max(monkeypatch):
    import app.bridge.throttler as mod

    fake_time = [0.0]
    monkeypatch.setattr(mod.time, "monotonic", lambda: fake_time[0])

    t = Throttler(reply_per_minute=20, init_max=3, init_window_sec=100, init_min_interval=0)
    for _ in range(3):
        fake_time[0] += 1
        assert await t.acquire(is_initiation=True) is True

    fake_time[0] += 1  # всё ещё в окне
    assert await t.acquire(is_initiation=True) is False

    fake_time[0] += 100  # окно прошло
    assert await t.acquire(is_initiation=True) is True
