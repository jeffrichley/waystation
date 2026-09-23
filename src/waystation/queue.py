"""Queue: runs submitted over time, run concurrently, each result as it completes.

``fan_out``'s open-ended peer. A batch is read whole before it starts; a queue
stays open, and a spec can be submitted to it whenever it becomes startable,
from any flow and against any host repo. Everything else a batch promises,
a queue keeps: one cap across everything submitted, every result yielded as
its run ends, a failure one more result and never a reason to cancel the rest,
and a block whose exit stops what is still going (ADR-0007, ADR-0047).
"""

from __future__ import annotations

import secrets
from functools import partial
from typing import Any

from waystation._serving import Queued, Serving
from waystation.errors import PreflightError
from waystation.flow import RunSpec
from waystation.ordering import ArrivalOrder, OrderingStrategy
from waystation.preflight import preflight
from waystation.results import Errored, Failure, RunFailed, RunResult

__all__ = ["QueuedRun", "RunQueue", "queue"]


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


class _QueuedRun[OutcomeT](Queued[RunResult[OutcomeT]]):
    """One submitted run: the spec it was submitted as, and the way to stop it.

    ``submit`` makes one; it is private as the queue itself is. ``cancel``
    stops this run alone: in flight, it keeps what its agent left and tears
    its sandbox down (ADR-0017); still queued, it never starts.
    """

    def __init__(self, spec: RunSpec[OutcomeT]) -> None:
        super().__init__()
        self.spec = spec


class _RunQueue[OutcomeT](Serving[_QueuedRun[OutcomeT], RunResult[OutcomeT]]):
    """The runs submitted to one queue, yielded as they complete.

    Private, as fan-out's iterator is: a flow script names what it iterates,
    ``RunResult``, and annotates with ``RunQueue`` when it must. The pull,
    the drain on close and the stop that waits are ``Serving``'s, which a
    merge queue shares (ADR-0049).
    """

    def __init__(
        self,
        max_concurrency: int | None,
        order: OrderingStrategy[_QueuedRun[OutcomeT]],
    ) -> None:
        super().__init__(
            max_concurrency,
            order,
            closed_message="this queue is closed: it takes no more runs",
        )

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
        return self.start(
            _QueuedRun(spec),
            partial(self._run, spec, preflighted=preflighted),
            partial(_refused, spec),
        )

    @staticmethod
    async def _run(
        spec: RunSpec[OutcomeT], *, preflighted: bool
    ) -> RunResult[OutcomeT]:
        """One run, begun once the queue pulls it.

        Until then it has no workspace, so its base ref resolves when it
        starts, not when it was submitted.
        """
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
