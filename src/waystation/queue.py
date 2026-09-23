"""Queue: runs submitted over time, run concurrently, each result as it completes.

``fan_out``'s open-ended peer. A batch is read whole before it starts; a queue
stays open, and a spec can be submitted to it whenever it becomes startable,
from any flow and against any host repo. Everything else a batch promises,
a queue keeps: one cap across everything submitted, every result yielded as
its run ends, a failure one more result and never a reason to cancel the rest,
and a block whose exit stops what is still going (ADR-0007, ADR-0047).
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Iterable
from typing import Any, Self

from waystation._cancellation import run_to_end
from waystation.errors import PreflightError
from waystation.flow import RunSpec
from waystation.ordering import ArrivalOrder, OrderingStrategy
from waystation.preflight import preflight
from waystation.results import Errored, Failure, RunFailed, RunResult

__all__ = ["QueuedRun", "RunQueue", "queue"]


# The event loop holds tasks weakly, so a run whose queue was dropped
# mid-run would be collected mid-run; each run is held here until it ends
# (the asyncio.create_task docs).
_UNFINISHED: set[asyncio.Task[Any]] = set()


async def _stop(tasks: Iterable[asyncio.Task[Any]]) -> None:
    """Cancel ``tasks``, and return once each has wound down.

    A run in flight keeps what its agent left and tears its sandbox down
    before its cancellation finishes (ADR-0017); a queued run never begins.
    Cancelling the caller meanwhile waits for that too and is raised
    afterwards, the way ``asyncio.TaskGroup`` leaves.
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


def _refused(spec: RunSpec[Any], failure: Failure) -> RunFailed:
    """A run refused before it began, as a result.

    Its preflight failed, or the strategy ordering it did. No stage failed
    and none of its work exists, so ``stage`` is ``None`` and the run id is
    minted here, for an attempt that got no further.
    """
    return RunFailed(
        run_id=secrets.token_hex(4),
        name=spec.label,
        base_sha=None,
        elapsed={},
        agent=None,
        series=None,
        preserved=None,
        stage=None,
        failure=failure,
    )


class _QueuedRun[OutcomeT]:
    """One submitted run: the spec it was submitted as, and the way to stop it.

    ``submit`` makes one; it is private as the queue itself is.
    """

    _task: asyncio.Task[RunResult[OutcomeT]]

    def __init__(self, spec: RunSpec[OutcomeT]) -> None:
        self.spec = spec
        # True from the pull that starts it until it ends.
        self._holds_slot = False
        # Set when the queue pulls this run: ``None`` to start it, or the
        # failure it is refused with, when the strategy ordering it failed.
        self._turn: asyncio.Future[Failure | None] = (
            asyncio.get_running_loop().create_future()
        )

    async def cancel(self) -> None:
        """Stop this run alone, and return once it has wound down.

        In flight, it keeps what its agent left and tears its sandbox down
        (ADR-0017); still queued, it never starts. Either way it reports
        nothing, and every other run goes on. A run that has already ended
        is left as it was: its result is yielded as it would have been.
        """
        await _stop([self._task])


class _RunQueue[OutcomeT]:
    """The runs submitted to one queue, yielded as they complete.

    Private, as fan-out's iterator is: a flow script names what it iterates,
    ``RunResult``, and annotates with ``RunQueue`` when it must.
    """

    def __init__(
        self,
        max_concurrency: int | None,
        order: OrderingStrategy[_QueuedRun[OutcomeT]],
    ) -> None:
        self._room = max_concurrency
        self._order = order
        self._in_flight = 0
        # Oldest first: what the strategy is offered at each pull.
        self._waiting: list[_QueuedRun[OutcomeT]] = []
        self._pull_due = False
        self._running: set[asyncio.Task[RunResult[OutcomeT]]] = set()
        # ``None`` is the end: put once, when the queue is closed and nothing
        # is running, and put back by each consumer that reads it.
        self._finished: asyncio.Queue[asyncio.Task[RunResult[OutcomeT]] | None] = (
            asyncio.Queue()
        )
        self._closed = False

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Close the queue, stop the runs still going, and wait for each."""
        self.close()
        await _stop(self._running)

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> RunResult[OutcomeT]:
        while True:
            # A cancelled wait takes nothing: the result stays for the next.
            task = await self._finished.get()
            if task is None:
                self._finished.put_nowait(None)  # every consumer sees the end
                raise StopAsyncIteration
            # A run stopped by cancel() or the block's exit reports nothing
            # (ADR-0017).
            if not task.cancelled():
                return task.result()

    def submit(
        self, spec: RunSpec[OutcomeT], *, preflighted: bool = False
    ) -> _QueuedRun[OutcomeT]:
        """Submit ``spec``; it starts once the queue has room and pulls it.

        With room for everything waiting, that is the loop's next turn;
        otherwise the queue's ordering strategy says which waiting run takes
        each slot as it frees (ADR-0048).

        It is preflighted when it starts, not now, so a queue that runs for
        hours checks each run against the host as it is then. A run that
        fails its preflight is one more result: a ``RunFailed`` whose
        ``stage`` is ``None``, since it never began.

        Args:
            spec: The run to perform — from any flow, against any host repo.
            preflighted: ``spec`` belongs to a batch that already passed
                ``preflight``, so its run skips the check, as
                ``spec.perform(preflighted=True)`` does (ADR-0032).

        Returns:
            The submitted run, to stop it alone by.

        Raises:
            RuntimeError: When the queue is closed, or its block was left.
        """
        if self._closed:
            msg = "this queue is closed: it takes no more runs"
            raise RuntimeError(msg)
        queued = _QueuedRun(spec)
        queued._task = task = asyncio.create_task(
            self._run(queued, preflighted=preflighted)
        )
        self._running.add(task)
        _UNFINISHED.add(task)
        task.add_done_callback(lambda _: self._ended(queued))
        self._waiting.append(queued)
        if not self._pull_due:
            # On the loop's next turn, not now, so runs submitted together
            # are ranked together: six ready and room for three starts the
            # best three, not the first three submitted.
            self._pull_due = True
            asyncio.get_running_loop().call_soon(self._pull)
        return queued

    def close(self) -> None:
        """Take no more runs; iteration ends once every submitted run has reported.

        Nothing is stopped: the runs submitted go on, queued ones included.
        Closing twice is closing once.
        """
        if not self._closed:
            self._closed = True
            self._end_if_drained()

    def _ended(self, queued: _QueuedRun[OutcomeT]) -> None:
        task = queued._task
        _UNFINISHED.discard(task)
        self._running.discard(task)
        # Its result first: the pull runs the caller's strategy, and nothing
        # it raises may cost this run its report.
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
        """Start waiting runs while there is room, asking the strategy which.

        The strategy is asked only when there is a choice — more waiting than
        room — so a queue with no cap never asks it. When it fails, every run
        it was ordering is refused with that failure, and the queue goes on
        taking runs (ADR-0016, ADR-0048).
        """
        self._pull_due = False
        # A run stopped while it waited is not offered, even before it ends.
        self._waiting = [
            run
            for run in self._waiting
            if not (run._task.done() or run._task.cancelling())
        ]
        while self._waiting:
            room = None if self._room is None else self._room - self._in_flight
            if room is not None and room < 1:
                return
            waiting = tuple(self._waiting)
            if room is None or len(waiting) <= room:
                # No choice to make: everything waiting starts.
                self._waiting.clear()
                for run in waiting:
                    self._start(run)
                return
            try:
                at = self._pick(waiting)
            except Exception as error:  # a failure is a value (ADR-0016)
                self._waiting.clear()
                for run in waiting:
                    run._turn.set_result(Errored(error))
                return
            self._start(self._waiting.pop(at))

    def _pick(self, waiting: tuple[_QueuedRun[OutcomeT], ...]) -> int:
        """Where the strategy's choice sits in ``waiting``: itself, not an equal."""
        chosen = self._order.pick(waiting)
        for at, run in enumerate(waiting):
            if run is chosen:
                return at
        msg = f"the ordering strategy picked {chosen!r}, not a waiting run"
        raise ValueError(msg)

    def _start(self, run: _QueuedRun[OutcomeT]) -> None:
        self._in_flight += 1
        run._holds_slot = True
        run._turn.set_result(None)

    async def _run(
        self, queued: _QueuedRun[OutcomeT], *, preflighted: bool
    ) -> RunResult[OutcomeT]:
        """One run, begun once the queue pulls it.

        Until then it has no workspace, so its base ref resolves when it
        starts, not when it was submitted.
        """
        spec = queued.spec
        refusal = await queued._turn
        if refusal is not None:
            return _refused(spec, refusal)
        if not preflighted:
            try:
                await preflight((spec,))
            except PreflightError as error:
                return _refused(
                    spec,
                    error.failure if error.failure is not None else Errored(error),
                )
        return await spec.perform(preflighted=True)


type RunQueue[OutcomeT] = _RunQueue[OutcomeT]
"""What ``queue()`` returns, for annotating a helper that takes one.

The class itself stays private, as fan-out's iterator and the stage runner
do (ADR-0032): an alias names a queue without making one constructible any
way but ``queue()``.
"""

type QueuedRun[OutcomeT] = _QueuedRun[OutcomeT]
"""What ``submit()`` returns, for annotating what holds one."""


def queue(
    *,
    max_concurrency: int | None = None,
    order: OrderingStrategy[QueuedRun[Any]] | None = None,
) -> RunQueue[Any]:
    """An open queue of runs: submit specs over time; yield each result as it completes.

    ``fan_out`` for work that arrives over time — a run that becomes
    startable only once an earlier one ends, over minutes or hours. Each
    submitted run is preflighted as it starts, then each result is yielded
    the moment its run ends — ``RunSucceeded``, ``RunConflicted`` or
    ``RunFailed``. A failing run is one more result: nothing is cancelled for
    it and nothing raises mid-iteration (ADR-0007).

    A queue is a block::

        async with queue(max_concurrency=4) as runs:
            runs.submit(first)
            async for result in runs:
                for spec in startable_now(result):
                    runs.submit(spec)
                if nothing_left():
                    runs.close()

    ``close()`` takes no more runs, and iteration ends once every run
    submitted has reported; until then an idle queue waits for the next
    submission. ``submit`` returns the run, whose ``await run.cancel()``
    stops that one alone. Leaving the block, however it is left, closes the
    queue, cancels the runs still in flight and returns only once each has
    kept its work and torn its sandbox down (ADR-0017); a queued run never
    starts, and a stopped run reports nothing.

    Args:
        max_concurrency: The most runs in flight at once, across everything
            submitted; ``None``, the default, is no limit. A queued run
            starts as a slot frees, and resolves its base ref then.
        order: Which waiting run starts next, when more are waiting than
            there is room for: asked at each such pull, with every waiting
            ``QueuedRun`` oldest first. ``None``, the default, is arrival
            order. A strategy that fails refuses the runs it was ordering,
            each a ``RunFailed`` whose ``stage`` is ``None`` (ADR-0048).

    Returns:
        An async iterator of results, and an async context manager that
        yields it.

    Raises:
        ValueError: At the call, when ``max_concurrency`` is below 1.
    """
    if max_concurrency is not None and max_concurrency < 1:
        msg = f"max_concurrency must be at least 1, or None, got {max_concurrency}"
        raise ValueError(msg)
    return _RunQueue(max_concurrency, order if order is not None else ArrivalOrder())
