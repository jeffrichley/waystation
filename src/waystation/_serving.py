"""What a queue of runs and a merge queue share: a block that serves work over time.

Work is submitted whenever it is ready and waits for room; each time there is
room the queue pulls, asking its ordering strategy which waiting item goes
when there is a choice (ADR-0048); each result is yielded as its work ends;
``close()`` takes no more, and leaving the block stops what is still going and
waits for it to wind down (ADR-0047). ``queue()`` is this with runs;
``merge_queue()`` is this with candidates and room for one (ADR-0049). One
copy, because the pull, the drain on close, the stop that waits and the
cancelled wait that loses nothing are each a place to get it subtly wrong.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from typing import Any, Self

from waystation._cancellation import run_to_end
from waystation.ordering import OrderingStrategy
from waystation.results import Errored, Failure

__all__ = ["Queued", "Serving", "stop"]


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


class Queued[ResultT]:
    """One submitted piece of work, and the way to stop it alone.

    A queue's own handle adds what it was submitted as — a run's ``spec``, a
    merge queue's ``candidate`` — which is what an ordering strategy reads.
    """

    _task: asyncio.Task[ResultT]

    def __init__(self) -> None:
        # True from the pull that starts it until it ends.
        self._holds_slot = False
        # Set when the queue pulls this: ``None`` to start it, or the failure
        # it is refused with, when the strategy ordering it failed.
        self._turn: asyncio.Future[Failure | None] = (
            asyncio.get_running_loop().create_future()
        )

    async def cancel(self) -> None:
        """Stop this alone, and return once it has wound down.

        In flight, it cleans up first (ADR-0017); still queued, it never
        starts. Either way it reports nothing, and everything else goes on.
        Work that has already ended is left as it was: its result is yielded
        as it would have been.
        """
        await stop([self._task])


class Serving[HandleT: Queued[Any], ResultT]:
    """Work submitted over time, pulled as there is room, yielded as it completes.

    A subclass names what it takes — ``submit`` is its own — and hands each
    handle to ``start`` with the work to do once pulled, and the result for
    one refused before it began.
    """

    def __init__(
        self,
        max_concurrency: int | None,
        order: OrderingStrategy[HandleT],
        *,
        closed_message: str,
    ) -> None:
        self._room = max_concurrency
        self._order = order
        self._closed_message = closed_message
        self._in_flight = 0
        # Oldest first: what the strategy is offered at each pull.
        self._waiting: list[HandleT] = []
        self._pull_due = False
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

    def start(
        self,
        queued: HandleT,
        work: Callable[[], Awaitable[ResultT]],
        refused: Callable[[Failure], ResultT],
    ) -> HandleT:
        """Queue ``work`` for ``queued``, begun once the queue pulls it.

        Raises:
            RuntimeError: When the queue is closed, or its block was left.
        """
        if self._closed:
            raise RuntimeError(self._closed_message)
        queued._task = task = asyncio.create_task(
            self._when_pulled(queued, work, refused)
        )
        self._running.add(task)
        _UNFINISHED.add(task)
        task.add_done_callback(lambda _: self._ended(queued))
        self._waiting.append(queued)
        if not self._pull_due:
            # On the loop's next turn, not now, so work submitted together is
            # ranked together: six ready and room for three starts the best
            # three, not the first three submitted (ADR-0048).
            self._pull_due = True
            asyncio.get_running_loop().call_soon(self._pull)
        return queued

    @staticmethod
    async def _when_pulled(
        queued: Queued[ResultT],
        work: Callable[[], Awaitable[ResultT]],
        refused: Callable[[Failure], ResultT],
    ) -> ResultT:
        refusal = await queued._turn
        if refusal is not None:
            return refused(refusal)
        return await work()

    def _ended(self, queued: HandleT) -> None:
        task = queued._task
        _UNFINISHED.discard(task)
        self._running.discard(task)
        # Its result first: the pull runs the caller's strategy, and nothing
        # it raises may cost this its report.
        self._finished.put_nowait(task)
        self._end_if_drained()
        if queued._holds_slot:
            # However it ended, even stopped before it could begin.
            queued._holds_slot = False
            self._in_flight -= 1
            self._pull()

    def _end_if_drained(self) -> None:
        # Once closed, nothing is added to _running, so this holds once.
        if self._closed and not self._running:
            self._finished.put_nowait(None)

    def _pull(self) -> None:
        """Start waiting work while there is room, asking the strategy which.

        The strategy is asked only when there is a choice — more waiting than
        room — so a queue with no cap never asks it. When it fails, everything
        it was ordering is refused with that failure, and the queue goes on
        taking work (ADR-0016, ADR-0048).
        """
        self._pull_due = False
        # Work stopped while it waited is not offered, even before it ends.
        self._waiting = [
            queued
            for queued in self._waiting
            if not (queued._task.done() or queued._task.cancelling())
        ]
        while self._waiting:
            room = None if self._room is None else self._room - self._in_flight
            if room is not None and room < 1:
                return
            waiting = tuple(self._waiting)
            if room is None or len(waiting) <= room:
                # No choice to make: everything waiting starts.
                self._waiting.clear()
                for queued in waiting:
                    self._admit(queued)
                return
            try:
                at = self._pick(waiting)
            except Exception as error:  # a failure is a value (ADR-0016)
                self._waiting.clear()
                for queued in waiting:
                    queued._turn.set_result(Errored(error))
                return
            self._admit(self._waiting.pop(at))

    def _pick(self, waiting: tuple[HandleT, ...]) -> int:
        """Where the strategy's choice sits in ``waiting``: itself, not an equal."""
        chosen = self._order.pick(waiting)
        for at, queued in enumerate(waiting):
            if queued is chosen:
                return at
        msg = f"the ordering strategy picked {chosen!r}, not a waiting item"
        raise ValueError(msg)

    def _admit(self, queued: HandleT) -> None:
        self._in_flight += 1
        queued._holds_slot = True
        queued._turn.set_result(None)
