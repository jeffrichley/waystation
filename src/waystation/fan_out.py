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

from waystation.flow import RunSpec, preflight
from waystation.results import RunResult

__all__ = ["fan_out"]


class _FanOut[OutcomeT]:
    """The runs of one batch, yielded as they complete."""

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
    """Run every spec in ``runs`` concurrently; yield each result as it completes."""
    if max_concurrency is not None and max_concurrency < 1:
        msg = f"max_concurrency must be at least 1, or None, got {max_concurrency}"
        raise ValueError(msg)
    return _FanOut(tuple(runs), max_concurrency)
