"""Injectable clocks so timeout tests advance time without wall sleeps."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Iterator
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Protocol

from waystation._cancellation import CommitPoints

__all__ = [
    "Clock",
    "ManualClock",
    "WallClock",
    "get_clock",
    "race_timeout",
    "use_clock",
]


class Clock(Protocol):
    """The time every bound in a run is measured against."""

    def monotonic(self) -> float:
        """Seconds on a clock that never goes backwards.

        Returns:
            The current reading; only the difference between two means anything.
        """
        ...

    async def sleep(self, seconds: float) -> None:
        """Return once ``seconds`` have passed on this clock.

        Args:
            seconds: How long to wait.
        """
        ...


@dataclass(slots=True)
class WallClock:
    """Production clock: real monotonic time and ``asyncio.sleep``."""

    def monotonic(self) -> float:
        """Real monotonic time.

        Returns:
            ``time.monotonic()``.
        """
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        """Sleep in real time.

        Args:
            seconds: How long to wait, passed to ``asyncio.sleep``.
        """
        await asyncio.sleep(seconds)


@dataclass(slots=True)
class ManualClock:
    """Deterministic clock for tests — ``advance`` wakes pending sleepers."""

    _now: float = 0.0
    _waiters: list[tuple[float, asyncio.Future[None]]] = field(default_factory=list)

    def monotonic(self) -> float:
        """The time ``advance`` has moved this clock to.

        Returns:
            Seconds advanced since the clock was made.
        """
        return self._now

    async def sleep(self, seconds: float) -> None:
        """Wait until ``advance`` moves the clock ``seconds`` past now.

        Args:
            seconds: How far the clock must move; zero or less only yields
                to the loop once.
        """
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
        """Move the clock forward, waking every sleeper whose time has come.

        Args:
            seconds: How far to move it.

        Raises:
            ValueError: When ``seconds`` is negative; this clock never goes back.
        """
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
    """The clock installed for the current context.

    Returns:
        The one ``use_clock`` installed, or the wall clock.
    """
    return _CLOCK.get() or _WALL


@contextlib.contextmanager
def use_clock(clock: Clock) -> Iterator[Clock]:
    """Install ``clock`` for the current context (tests).

    Args:
        clock: The clock every bound in this context measures against.

    Yields:
        ``clock``, until the block ends and the previous clock is back.
    """
    token = _CLOCK.set(clock)
    try:
        yield clock
    finally:
        _CLOCK.reset(token)


async def race_timeout[T](
    awaitable: asyncio.Future[T] | asyncio.Task[T],
    seconds: float | None,
    *,
    commits: CommitPoints | None = None,
) -> T:
    """Await ``awaitable``, cancelling it if ``seconds`` elapses on the active clock.

    With ``commits``, a bound that runs out waits for any commit point under
    way first, and the work that finished meanwhile is its result (ADR-0027).

    Args:
        awaitable: The work to bound; it is cancelled if the bound runs out.
        seconds: The bound; ``None`` means unbounded (ADR-0017).
        commits: The commit points a bound that runs out waits for.

    Returns:
        What ``awaitable`` returned.

    Raises:
        TimeoutError: When ``seconds`` elapsed first, and the work was
            cancelled.
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
        if commits is not None:
            await commits.idle()
            if task.done():
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
