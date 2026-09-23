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
from functools import partial
from typing import Any

from waystation._serving import Serving, stop
from waystation.errors import PreflightError
from waystation.flow import RunSpec
from waystation.preflight import preflight
from waystation.results import Errored, RunFailed, RunResult

__all__ = ["QueuedRun", "RunQueue", "queue"]


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
        await stop([self._task])


class _RunQueue[OutcomeT](Serving[RunResult[OutcomeT]]):
    """The runs submitted to one queue, yielded as they complete.

    Private, as fan-out's iterator is: a flow script names what it iterates,
    ``RunResult``, and annotates with ``RunQueue`` when it must. What it
    shares with a merge queue — the slots, the drain on close, the stop that
    waits — is ``Serving``'s.
    """

    def __init__(self, max_concurrency: int | None) -> None:
        super().__init__(
            max_concurrency,
            closed_message="this queue is closed: it takes no more runs",
        )

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
        task = self.start(partial(self._run, spec, preflighted=preflighted))
        return _QueuedRun(spec, task)

    @staticmethod
    async def _run(
        spec: RunSpec[OutcomeT], *, preflighted: bool
    ) -> RunResult[OutcomeT]:
        """One run, begun once a slot is free.

        Until then it has no workspace, so its base ref resolves when it
        starts, not when it was submitted.
        """
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
