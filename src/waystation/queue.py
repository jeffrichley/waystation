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
import contextlib
import secrets
from collections.abc import Iterable
from typing import Any, Self

from waystation._cancellation import run_to_end
from waystation.errors import PreflightError
from waystation.flow import RunSpec
from waystation.preflight import preflight
from waystation.results import Errored, RunFailed, RunResult

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


def _refused(spec: RunSpec[Any], error: PreflightError) -> RunFailed:
    """A run that failed its preflight, as a result: it never began.

    No stage failed and none of its work exists, so ``stage`` is ``None``
    and the run id is minted here, for an attempt that got no further.
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
        failure=error.failure if error.failure is not None else Errored(error),
    )


class _QueuedRun[OutcomeT]:
    """One submitted run: the spec it was submitted as, and the way to stop it.

    ``submit`` makes one; it is private as the queue itself is.
    """

    def __init__(
        self, spec: RunSpec[OutcomeT], task: asyncio.Task[RunResult[OutcomeT]]
    ) -> None:
        self.spec = spec
        self._task = task

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

    def __init__(self, max_concurrency: int | None) -> None:
        # FIFO, so queued runs start in the order submitted as slots free.
        self._slots = (
            asyncio.Semaphore(max_concurrency) if max_concurrency is not None else None
        )
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
        """Submit ``spec``; it starts as soon as a slot is free.

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
        task = asyncio.create_task(self._run(spec, preflighted=preflighted))
        self._running.add(task)
        _UNFINISHED.add(task)
        task.add_done_callback(self._ended)
        return _QueuedRun(spec, task)

    def close(self) -> None:
        """Take no more runs; iteration ends once every submitted run has reported.

        Nothing is stopped: the runs submitted go on, queued ones included.
        Closing twice is closing once.
        """
        if not self._closed:
            self._closed = True
            self._end_if_drained()

    def _ended(self, task: asyncio.Task[RunResult[OutcomeT]]) -> None:
        _UNFINISHED.discard(task)
        self._running.discard(task)
        self._finished.put_nowait(task)
        self._end_if_drained()

    def _end_if_drained(self) -> None:
        # Once closed, nothing is added to _running, so this holds once.
        if self._closed and not self._running:
            self._finished.put_nowait(None)

    async def _run(
        self, spec: RunSpec[OutcomeT], *, preflighted: bool
    ) -> RunResult[OutcomeT]:
        """One run, begun once a slot is free.

        Until then it has no workspace, so its base ref resolves when it
        starts, not when it was submitted.
        """
        async with self._slots or contextlib.nullcontext():
            if not preflighted:
                try:
                    await preflight((spec,))
                except PreflightError as error:
                    return _refused(spec, error)
            return await spec.perform(preflighted=True)


type RunQueue[OutcomeT] = _RunQueue[OutcomeT]
"""What ``queue()`` returns, for annotating a helper that takes one.

The class itself stays private, as fan-out's iterator and the stage runner
do (ADR-0032): an alias names a queue without making one constructible any
way but ``queue()``.
"""

type QueuedRun[OutcomeT] = _QueuedRun[OutcomeT]
"""What ``submit()`` returns, for annotating what holds one."""


def queue(*, max_concurrency: int | None = None) -> RunQueue[Any]:
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
            submitted; ``None``, the default, is no limit. Queued runs start
            in the order submitted as slots free, and each resolves its base
            ref then.

    Returns:
        An async iterator of results, and an async context manager that
        yields it.

    Raises:
        ValueError: At the call, when ``max_concurrency`` is below 1.
    """
    if max_concurrency is not None and max_concurrency < 1:
        msg = f"max_concurrency must be at least 1, or None, got {max_concurrency}"
        raise ValueError(msg)
    return _RunQueue(max_concurrency)
