"""Which queued run starts next: an ordering strategy the caller injects (#139).

A queue with room pulls one waiting run, and the strategy says which. It sees
the whole waiting set, in arrival order, at the moment of each pull, so a
ranking that changes as runs land is read as it is then. With none given the
queue pulls in arrival order, as it always did (ADR-0017, ADR-0048). A
strategy that fails is one more result for each run it was ordering, never a
raise (ADR-0016).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, cast

import pytest

from helpers import Gate, a_run, until_batch, why
from waystation import Errored, RunContext, RunFailed, RunSucceeded, queue
from waystation.ordering import ArrivalOrder, OrderingStrategy
from waystation.queue import QueuedRun


class _Ranked:
    """Pull the waiting run whose name ranks lowest, recording what it saw."""

    def __init__(self, rank: dict[str, int]) -> None:
        self.rank = rank
        self.seen: list[list[str]] = []

    def pick(self, waiting: Sequence[QueuedRun[Any]]) -> QueuedRun[Any]:
        self.seen.append([str(run.spec.label) for run in waiting])
        return min(waiting, key=lambda run: self.rank[str(run.spec.label)])


def _started(order: list[str]) -> Callable[[RunContext], None]:
    def record(ctx: RunContext) -> None:
        assert ctx.name is not None
        order.append(ctx.name)

    return record


@pytest.mark.git
async def test_with_no_strategy_queued_runs_start_in_arrival_order(
    host_repo: Path,
) -> None:
    gate, started = Gate(), list[str]()

    async with queue(max_concurrency=1) as runs:
        runs.submit(a_run(host_repo).name("first").on_run_start(lambda _: gate.hold()))
        for label in ("b", "c", "d"):
            runs.submit(a_run(host_repo).name(label).on_run_start(_started(started)))
        await until_batch(gate.reached.is_set, runs)
        gate.release.set()
        runs.close()
        results = [result async for result in runs]

    assert [type(result) for result in results] == [RunSucceeded] * 4, why(results)
    assert started == ["b", "c", "d"]


@pytest.mark.git
async def test_a_strategy_picks_each_run_that_starts_from_everything_waiting(
    host_repo: Path,
) -> None:
    gate, started = Gate(), list[str]()
    order = _Ranked({"b": 3, "c": 1, "d": 2})

    async with queue(max_concurrency=1, order=order) as runs:
        runs.submit(a_run(host_repo).name("first").on_run_start(lambda _: gate.hold()))
        await until_batch(gate.reached.is_set, runs)
        for label in ("b", "c", "d"):
            runs.submit(a_run(host_repo).name(label).on_run_start(_started(started)))
        gate.release.set()
        runs.close()
        results = [result async for result in runs]

    assert [type(result) for result in results] == [RunSucceeded] * 4, why(results)
    assert started == ["c", "d", "b"]
    # Every pull with a choice to make saw what was waiting then, oldest first.
    assert order.seen == [["b", "c", "d"], ["b", "d"]]


@pytest.mark.git
async def test_runs_submitted_together_are_ranked_together(host_repo: Path) -> None:
    """With room for fewer than were submitted, the best go first, not the first."""
    started: list[str] = []
    order = _Ranked({"a": 3, "b": 2, "c": 1})

    async with queue(max_concurrency=1, order=order) as runs:
        for label in ("a", "b", "c"):
            runs.submit(a_run(host_repo).name(label).on_run_start(_started(started)))
        runs.close()
        results = [result async for result in runs]

    assert [type(result) for result in results] == [RunSucceeded] * 3, why(results)
    assert started == ["c", "b", "a"]
    assert order.seen == [["a", "b", "c"], ["a", "b"]]


@pytest.mark.git
async def test_the_ranking_is_read_at_each_pull_not_at_submission(
    host_repo: Path,
) -> None:
    gate, started = Gate(), list[str]()
    order = _Ranked({"b": 1, "c": 2, "d": 3})

    def reranked(ctx: RunContext) -> None:
        _started(started)(ctx)
        order.rank["d"] = 0  # what "b" landing unblocked

    async with queue(max_concurrency=1, order=order) as runs:
        runs.submit(a_run(host_repo).name("first").on_run_start(lambda _: gate.hold()))
        await until_batch(gate.reached.is_set, runs)
        runs.submit(a_run(host_repo).name("b").on_run_start(reranked))
        for label in ("c", "d"):
            runs.submit(a_run(host_repo).name(label).on_run_start(_started(started)))
        gate.release.set()
        runs.close()
        results = [result async for result in runs]

    assert [type(result) for result in results] == [RunSucceeded] * 4, why(results)
    assert started == ["b", "d", "c"]


@pytest.mark.git
async def test_a_cancelled_queued_run_is_never_offered_to_the_strategy(
    host_repo: Path,
) -> None:
    gate = Gate()
    order = _Ranked({"b": 1, "c": 2, "d": 3})

    async with queue(max_concurrency=1, order=order) as runs:
        runs.submit(a_run(host_repo).name("first").on_run_start(lambda _: gate.hold()))
        await until_batch(gate.reached.is_set, runs)
        dropped = runs.submit(a_run(host_repo).name("b"))
        runs.submit(a_run(host_repo).name("c"))
        runs.submit(a_run(host_repo).name("d"))
        await dropped.cancel()
        gate.release.set()
        runs.close()
        results = [result async for result in runs]

    assert [type(result) for result in results] == [RunSucceeded] * 3, why(results)
    assert order.seen == [["c", "d"]]


@pytest.mark.git
async def test_a_strategy_is_asked_only_when_there_is_a_choice(host_repo: Path) -> None:
    order = _Ranked({})

    async with queue(max_concurrency=2, order=order) as runs:
        runs.submit(a_run(host_repo).name("a"))
        runs.submit(a_run(host_repo).name("b"))
        runs.close()
        results = [result async for result in runs]

    assert [type(result) for result in results] == [RunSucceeded] * 2, why(results)
    assert order.seen == []


class _Broken:
    """A strategy that raises ``fault``, or with none picks a run not waiting."""

    def __init__(self, fault: Exception | None = None) -> None:
        self.fault = fault

    def pick(self, waiting: Sequence[QueuedRun[Any]]) -> QueuedRun[Any]:
        if self.fault is not None:
            raise self.fault
        return cast("QueuedRun[Any]", object())  # waiting nowhere


@pytest.mark.git
@pytest.mark.parametrize(
    ("order", "error"),
    [
        (_Broken(LookupError("no rank for this ticket")), LookupError),
        (_Broken(), ValueError),
    ],
    ids=["raises", "picks-a-run-not-waiting"],
)
async def test_a_failing_strategy_refuses_what_it_was_ordering_and_the_queue_goes_on(
    host_repo: Path, order: _Broken, error: type[Exception]
) -> None:
    gate, started = Gate(), list[str]()

    async with queue(max_concurrency=1, order=order) as runs:
        runs.submit(a_run(host_repo).name("first").on_run_start(lambda _: gate.hold()))
        await until_batch(gate.reached.is_set, runs)
        for label in ("b", "c"):
            runs.submit(a_run(host_repo).name(label).on_run_start(_started(started)))
        gate.release.set()
        results = [await anext(runs) for _ in range(3)]
        # Still serving: a run submitted into room it has is never ordered.
        runs.submit(a_run(host_repo).name("later"))
        runs.close()
        results.extend([result async for result in runs])

    refused = [result for result in results if isinstance(result, RunFailed)]
    assert sorted(result.name or "" for result in refused) == ["b", "c"]
    for result in refused:
        assert result.stage is None, "it never began"
        assert isinstance(result.failure, Errored)
        assert isinstance(result.failure.exception, error)
    assert started == []
    assert sorted(
        result.name or "" for result in results if isinstance(result, RunSucceeded)
    ) == ["first", "later"]


@pytest.mark.unit
def test_arrival_order_pulls_the_oldest_waiting_item() -> None:
    order: OrderingStrategy[str] = ArrivalOrder()
    assert order.pick(["oldest", "newer", "newest"]) == "oldest"
