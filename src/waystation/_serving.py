"""What a queue of runs and a merge queue share: a block that serves work over time.

Work is submitted whenever it is ready, starts as a slot frees in the order it
came, and each result is yielded as its work ends; ``close()`` takes no more,
and leaving the block stops what is still going and waits for it to wind down
(ADR-0047). ``queue()`` is this with runs; ``merge_queue()`` is this with
candidates and one slot (ADR-0048). One copy, because the drain on close, the
stop that waits, and the cancelled wait that loses nothing are each a place to
get it subtly wrong.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Iterable
from typing import Any, Self

from waystation._cancellation import run_to_end

__all__ = ["Serving", "stop"]


# The event loop holds tasks weakly, so work whose queue was dropped mid-way
# would be collected mid-way; each piece is held here until it ends (the
# asyncio.create_task docs).
_UNFINISHED: set[asyncio.Task[Any]] = set()


async def stop(tasks: Iterable[asyncio.Task[Any]]) -> None:
    """Cancel ``tasks``, and return once each has wound down.

    Work in flight cleans up before its cancellation finishes (ADR-0017);
    queued work never begins. Cancelling the caller meanwhile waits for that
    too and is raised afterwards, the way ``asyncio.TaskGroup`` leaves.
    """
    tasks = list(tasks)
    for task in tasks:
        task.cancel()
    if not tasks:
        return
    waited: list[asyncio.CancelledError] = []
    await run_to_end(asyncio.wait(tasks), waited.append)
    if waited:
        raise waited[0]


class Serving[ResultT]:
    """Work submitted over time, each result yielded as it completes.

    A subclass names what it takes — ``submit`` is its own — and hands each
    piece to ``start`` as the work to do once a slot is free.
    """

    def __init__(self, max_concurrency: int | None, *, closed_message: str) -> None:
        # FIFO, so queued work starts in the order submitted as slots free.
        self._slots = (
            asyncio.Semaphore(max_concurrency) if max_concurrency is not None else None
        )
        self._closed_message = closed_message
        self._running: set[asyncio.Task[ResultT]] = set()
        # ``None`` is the end: put once, when the queue is closed and nothing
        # is running, and put back by each consumer that reads it.
        self._finished: asyncio.Queue[asyncio.Task[ResultT] | None] = asyncio.Queue()
        self._closed = False

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Close the queue, stop the work still going, and wait for each."""
        self.close()
        await stop(self._running)

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> ResultT:
        while True:
            # A cancelled wait takes nothing: the result stays for the next.
            task = await self._finished.get()
            if task is None:
                self._finished.put_nowait(None)  # every consumer sees the end
                raise StopAsyncIteration
            # Work stopped by its handle or the block's exit reports nothing
            # (ADR-0017).
            if not task.cancelled():
                return task.result()

    def close(self) -> None:
        """Take no more; iteration ends once everything submitted has reported.

        Nothing is stopped: what was submitted goes on, queued work included.
        Closing twice is closing once.
        """
        if not self._closed:
            self._closed = True
            self._end_if_drained()

    def start(self, work: Callable[[], Awaitable[ResultT]]) -> asyncio.Task[ResultT]:
        """Queue ``work``, begun once a slot is free; the task that does it.

        Raises:
            RuntimeError: When the queue is closed, or its block was left.
        """
        if self._closed:
            raise RuntimeError(self._closed_message)
        task = asyncio.create_task(self._slotted(work))
        self._running.add(task)
        _UNFINISHED.add(task)
        task.add_done_callback(self._ended)
        return task

    async def _slotted(self, work: Callable[[], Awaitable[ResultT]]) -> ResultT:
        async with self._slots or contextlib.nullcontext():
            return await work()

    def _ended(self, task: asyncio.Task[ResultT]) -> None:
        _UNFINISHED.discard(task)
        self._running.discard(task)
        self._finished.put_nowait(task)
        self._end_if_drained()

    def _end_if_drained(self) -> None:
        # Once closed, nothing is added to _running, so this holds once.
        if self._closed and not self._running:
            self._finished.put_nowait(None)
