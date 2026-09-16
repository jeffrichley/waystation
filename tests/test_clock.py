"""Unit tests for injectable clocks and race_timeout."""

from __future__ import annotations

import asyncio

import pytest

from waystation.clock import ManualClock, WallClock, get_clock, race_timeout, use_clock


@pytest.mark.unit
@pytest.mark.asyncio
async def test_manual_clock_advance_wakes_sleepers() -> None:
    clock = ManualClock()
    with use_clock(clock):
        assert get_clock() is clock
        task = asyncio.create_task(clock.sleep(2.0))
        for _ in range(20):
            if clock._waiters:
                break
            await asyncio.sleep(0)
        clock.advance(2.0)
        await task
        assert clock.monotonic() == 2.0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_manual_clock_rejects_negative_advance() -> None:
    clock = ManualClock()
    with pytest.raises(ValueError):
        clock.advance(-1.0)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_race_timeout_none_is_unbounded() -> None:
    async def _ok() -> str:
        return "ok"

    task = asyncio.create_task(_ok())
    assert await race_timeout(task, None) == "ok"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_race_timeout_cancels_on_manual_clock() -> None:
    clock = ManualClock()

    async def _hang() -> None:
        await clock.sleep(100.0)

    with use_clock(clock):
        task = asyncio.create_task(_hang())
        for _ in range(50):
            if clock._waiters:
                break
            await asyncio.sleep(0)
        race = asyncio.create_task(race_timeout(task, 1.0))
        for _ in range(50):
            if len(clock._waiters) >= 2 or race.done():
                break
            await asyncio.sleep(0)
        clock.advance(1.0)
        with pytest.raises(TimeoutError):
            await race


@pytest.mark.unit
@pytest.mark.asyncio
async def test_wall_clock_sleep_and_monotonic() -> None:
    clock = WallClock()
    t0 = clock.monotonic()
    await clock.sleep(0)
    assert clock.monotonic() >= t0
    assert get_clock() is not None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_race_timeout_task_wins_before_bound() -> None:
    clock = ManualClock()

    async def _quick() -> str:
        await clock.sleep(0.5)
        return "done"

    with use_clock(clock):
        task = asyncio.create_task(_quick())
        race = asyncio.create_task(race_timeout(task, 10.0))
        for _ in range(50):
            if clock._waiters:
                break
            await asyncio.sleep(0)
        clock.advance(0.5)
        assert await race == "done"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_race_timeout_accepts_future() -> None:
    clock = ManualClock()
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[str] = loop.create_future()

    with use_clock(clock):
        race = asyncio.create_task(race_timeout(fut, 1.0))
        for _ in range(50):
            if clock._waiters:
                break
            await asyncio.sleep(0)
        fut.set_result("from-future")
        # Advance past the bound so the sleeper settles if still pending.
        clock.advance(0.0)
        assert await race == "from-future"
