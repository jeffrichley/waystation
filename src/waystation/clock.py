"""Injectable clocks so timeout tests advance time without wall sleeps."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Iterator
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Protocol


class Clock(Protocol):
    def monotonic(self) -> float: ...

    async def sleep(self, seconds: float) -> None: ...


@dataclass(slots=True)
class WallClock:
    """Production clock: real monotonic time and ``asyncio.sleep``."""

    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


@dataclass(slots=True)
class ManualClock:
    """Deterministic clock for tests — ``advance`` wakes pending sleepers."""

    _now: float = 0.0
    _waiters: list[tuple[float, asyncio.Future[None]]] = field(default_factory=list)

    def monotonic(self) -> float:
        return self._now

    async def sleep(self, seconds: float) -> None:
        if seconds <= 0:
            await asyncio.sleep(0)
            return
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[None] = loop.create_future()
        deadline = self._now + seconds
        self._waiters.append((deadline, fut))
        try:
            await fut
        finally:
            self._waiters = [(d, f) for d, f in self._waiters if f is not fut]

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            msg = f"seconds must be >= 0, got {seconds!r}"
            raise ValueError(msg)
        self._now += seconds
        ready = [(d, f) for d, f in self._waiters if d <= self._now and not f.done()]
        for _, fut in ready:
            fut.set_result(None)


_CLOCK: ContextVar[Clock | None] = ContextVar("waystation_clock", default=None)
_WALL = WallClock()


def get_clock() -> Clock:
    return _CLOCK.get() or _WALL


@contextlib.contextmanager
def use_clock(clock: Clock) -> Iterator[Clock]:
    """Install ``clock`` for the current context (tests)."""
    token = _CLOCK.set(clock)
    try:
        yield clock
    finally:
        _CLOCK.reset(token)


async def race_timeout[T](
    awaitable: asyncio.Future[T] | asyncio.Task[T],
    seconds: float | None,
) -> T:
    """Await ``awaitable``, cancelling it if ``seconds`` elapses on the active clock.

    ``seconds is None`` means unbounded. Raises ``TimeoutError`` on expiry.
    """
    if seconds is None:
        return await awaitable

    clock = get_clock()
    task: asyncio.Task[T]
    if isinstance(awaitable, asyncio.Task):
        task = awaitable
    else:
        task = asyncio.ensure_future(awaitable)
    sleeper = asyncio.create_task(clock.sleep(seconds), name="waystation-timeout")
    try:
        done, _pending = await asyncio.wait(
            {task, sleeper},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if task in done:
            return task.result()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        raise TimeoutError()
    finally:
        if not sleeper.done():
            sleeper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sleeper
