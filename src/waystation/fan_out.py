"""Fan-out: a batch of runs, run concurrently, each result as it completes.

A batch is heterogeneous by design — different agents, sandboxes, targets,
even different flows and host repos — and every run in it reports: a failure
is one more result, never a reason to cancel the rest (ADR-0007).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterable
from typing import Self

from waystation._cancellation import run_to_end
from waystation._preflight import preflight
from waystation.flow import RunSpec
from waystation.results import RunResult

__all__ = ["fan_out"]


class _FanOut[OutcomeT]:
    """The runs of one batch, yielded as they complete.

    Private, as ``asyncio.as_completed``'s iterator is: a flow script names
    what it iterates, ``RunResult``, never the iterator itself.
    """

    def __init__(
        self, specs: tuple[RunSpec[OutcomeT], ...], max_concurrency: int | None
    ) -> None:
        self._specs = specs
        # FIFO, so queued runs start in batch order as slots free.
        self._slots = (
            asyncio.Semaphore(max_concurrency) if max_concurrency is not None else None
        )
        self._runs: list[asyncio.Task[RunResult[OutcomeT]]] | None = None
        self._finished: asyncio.Queue[asyncio.Task[RunResult[OutcomeT]]] = (
            asyncio.Queue()
        )
        self._yielded = 0

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
        runs = self._runs or []
        for run in runs:
            run.cancel()
        if not runs:
            return
        waited: list[asyncio.CancelledError] = []
        await run_to_end(asyncio.wait(runs), waited.append)
        if waited:
            raise waited[0]

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> RunResult[OutcomeT]:
        runs = await self._start()
        if self._yielded == len(runs):
            raise StopAsyncIteration
        run = await self._finished.get()
        self._yielded += 1
        return run.result()

    async def _start(self) -> list[asyncio.Task[RunResult[OutcomeT]]]:
        """Preflight the batch whole, then start its runs; once."""
        if self._runs is None:
            await preflight(self._specs)
            self._runs = [asyncio.create_task(self._run(spec)) for spec in self._specs]
            for run in self._runs:
                run.add_done_callback(self._finished.put_nowait)
        return self._runs

    async def _run(self, spec: RunSpec[OutcomeT]) -> RunResult[OutcomeT]:
        """One run, begun once a slot is free: until then it has no workspace,
        so its base ref resolves when it starts, not when the batch did."""
        async with self._slots or contextlib.nullcontext():
            # The batch was preflighted whole, once per distinct spec.
            return await spec._execute(preflighted=True)


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
    if max_concurrency is not None and max_concurrency < 1:
        msg = f"max_concurrency must be at least 1, or None, got {max_concurrency}"
        raise ValueError(msg)
    return _FanOut(tuple(runs), max_concurrency)
