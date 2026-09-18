"""Work a cancellation must not cut short: awaited to its end, never lost."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from contextvars import ContextVar
from typing import Any


async def run_to_end[T](
    work: Awaitable[T], on_cancel: Callable[[asyncio.CancelledError], None]
) -> T:
    """Await ``work`` to its end, handing each cancellation meanwhile to ``on_cancel``.

    The caller decides when a cancellation that waited is raised — it never
    decides it away. If the work itself is cancelled, that goes on at once.
    """
    task = asyncio.ensure_future(work)
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError as cancel:
            if task.cancelled():
                raise
            on_cancel(cancel)


class CommitPoints:
    """The commit points under way inside one bounded piece of work.

    A bound that runs out waits for them to end before it cancels the work,
    so it never fires into a write that must finish (ADR-0027).
    """

    def __init__(self) -> None:
        self._under_way = 0
        self._idle = asyncio.Event()
        self._idle.set()

    def enter(self) -> None:
        self._under_way += 1
        self._idle.clear()

    def leave(self) -> None:
        self._under_way -= 1
        if not self._under_way:
            self._idle.set()

    async def idle(self) -> None:
        await self._idle.wait()


_POINTS: ContextVar[CommitPoints | None] = ContextVar(
    "waystation_commit_points", default=None
)


def start_bounded[T](
    work: Coroutine[Any, Any, T],
) -> tuple[asyncio.Task[T], CommitPoints]:
    """Start ``work`` as a task whose commit points its bound can see."""
    points = CommitPoints()
    token = _POINTS.set(points)
    try:
        return asyncio.create_task(work), points
    finally:
        _POINTS.reset(token)


async def committed[T](work: Awaitable[T]) -> T:
    """Run ``work``, a write that must not be cut short, to its end.

    A cancellation meanwhile waits for it and is raised afterwards, never
    spent; a bound around it waits for it before firing (ADR-0027).
    """
    points = _POINTS.get()
    if points is not None:
        points.enter()
    waited: list[asyncio.CancelledError] = []
    try:
        result = await run_to_end(work, waited.append)
    except Exception:
        if waited:
            raise waited[0] from None
        raise
    finally:
        if points is not None:
            points.leave()
    if waited:
        raise waited[0]
    return result
