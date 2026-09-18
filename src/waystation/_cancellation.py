"""Work a cancellation must not cut short: awaited to its end, never lost."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable


async def run_to_end[T](
    work: Awaitable[T], on_cancel: Callable[[asyncio.CancelledError], None]
) -> T:
    """Await ``work`` to its end, handing each cancellation meanwhile to ``on_cancel``.

    The caller decides what a cancellation that waited means: held and raised
    later (ADR-0017), or spent on a result it could not stop (ADR-0027). If
    the work itself is cancelled, that goes on at once.
    """
    task = asyncio.ensure_future(work)
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError as cancel:
            if task.cancelled():
                raise
            on_cancel(cancel)
