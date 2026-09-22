"""Fan-out: a batch of runs, run concurrently, each result as it completes.

A batch is heterogeneous by design — different agents, sandboxes, targets,
even different flows and host repos — and every run in it reports: a failure
is one more result, never a reason to cancel the rest (ADR-0007).
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from typing import Self

from waystation.flow import RunSpec
from waystation.preflight import preflight
from waystation.queue import RunQueue, queue
from waystation.results import RunResult

__all__ = ["fan_out"]


class _FanOut[OutcomeT]:
    """The runs of one batch, yielded as they complete.

    A batch is a queue whose runs were all submitted at once, after one
    preflight of the whole, and which was closed at once: what a user's own
    scheduler would write on ``queue()``, so fan-out is written the same way.

    Private, as ``asyncio.as_completed``'s iterator is: a flow script names
    what it iterates, ``RunResult``, never the iterator itself.
    """

    def __init__(
        self, specs: tuple[RunSpec[OutcomeT], ...], runs: RunQueue[OutcomeT]
    ) -> None:
        self._specs = specs
        self._runs = runs
        # Consumers that ask at once start the batch once.
        self._starting = asyncio.Lock()
        self._started = False

    async def __aenter__(self) -> Self:
        await self._start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Stop the runs still going, and return once each has wound down.

        A run in flight keeps what its agent left and tears its sandbox down
        before its cancellation finishes (ADR-0017); a queued run never
        begins. Cancelling the consumer meanwhile waits for that too and is
        raised afterwards, the way ``asyncio.TaskGroup`` leaves.
        """
        await self._runs.__aexit__(*exc)

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> RunResult[OutcomeT]:
        await self._start()
        return await anext(self._runs)

    async def _start(self) -> None:
        """Preflight the batch whole, then submit its runs and close; once."""
        async with self._starting:
            if not self._started:
                await preflight(self._specs)
                for spec in self._specs:
                    # The batch was preflighted whole, once per distinct spec:
                    # the one thing fan-out does that a queue of the same
                    # specs does not.
                    self._runs.submit(spec, preflighted=True)
                self._runs.close()
                self._started = True


def fan_out[OutcomeT](
    runs: Iterable[RunSpec[OutcomeT]], *, max_concurrency: int | None = None
) -> _FanOut[OutcomeT]:
    """Run every spec in ``runs`` concurrently; yield each result as it completes.

    ``runs`` is read to its end here, before anything starts. The batch is
    preflighted as a whole when iteration (or the block) begins: each
    distinct agent provider and sandbox spec once, every prompt file, and the
    host's git. Then every run starts, and each result is yielded the moment
    its run ends — ``RunSucceeded``, ``RunConflicted`` or ``RunFailed``. A
    failing run is one more result: nothing is cancelled for it and nothing
    raises mid-iteration (ADR-0007).

    To consume a whole batch::

        async for result in fan_out(batch):
            ...

    To be able to stop early, open it as a block::

        async with fan_out(batch) as results:
            async for result in results:
                if good_enough(result):
                    break

    Leaving the block, however it is left, cancels the runs still in flight
    and returns only once each has kept its work and torn its sandbox down
    (ADR-0017); a queued run never starts. Leaving a bare ``async for``
    early stops nothing, as with ``asyncio.as_completed``: the rest run on
    unobserved until they end, or until ``asyncio.run`` cancels them as it
    closes the loop.

    Args:
        runs: The batch — run specs from any flows, against any host repos.
        max_concurrency: The most runs in flight at once; ``None``, the
            default, is no limit. A queued run starts as a slot frees and
            resolves its base ref then, not when the batch began.

    Returns:
        An async iterator of results, and an async context manager that
        yields it.

    Raises:
        PreflightError: Before the first result — from the first ``__anext__``
            or from entering the block — when the batch fails preflight. No
            run has started.
        ValueError: At the call, when ``max_concurrency`` is below 1.
    """
    # Made now, so a cap that would start nothing is refused at the call.
    return _FanOut(tuple(runs), queue(max_concurrency=max_concurrency))
