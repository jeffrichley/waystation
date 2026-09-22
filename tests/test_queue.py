"""A queue: runs submitted over time, each result yielded as it completes (#113).

``fan_out``'s open-ended peer. A queue stays open, takes a spec whenever one
is submitted, preflights each run as it starts, caps what is in flight across
everything submitted, and yields every typed result as its run ends — a
failure is one more result, never a raise (ADR-0007). ``close()`` says no
more is coming, and iteration ends once everything submitted has reported.
Leaving the block stops the runs in flight, which keep their work and tear
down (ADR-0017), and never starts the queued ones. One submitted run can be
stopped alone, through the handle ``submit`` returns.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from helpers import (
    PROMPT,
    Gate,
    GatedSandbox,
    Stuck,
    a_run,
    branches,
    init_host_repo,
    subjects,
    until,
    until_batch,
    why,
    workspaces,
)
from waystation import (
    HookRaised,
    RunContext,
    RunFailed,
    RunResult,
    RunSucceeded,
    Summary,
    queue,
)
from waystation.queue import RunQueue


@pytest.mark.git
async def test_a_run_submitted_after_others_reported_still_runs_and_reports(
    host_repo: Path, tmp_path: Path
) -> None:
    (tmp_path / "other").mkdir()
    other_repo = init_host_repo(tmp_path / "other")
    results: list[RunResult[Any]] = []

    async with queue() as runs:
        runs.submit(a_run(host_repo))
        results.append(await anext(runs))
        # Work that became startable only once the first run ended.
        runs.submit(a_run(other_repo))
        runs.close()
        results.extend([result async for result in runs])

    assert [type(result) for result in results] == [RunSucceeded] * 2, why(results)
    assert len(branches(host_repo, "waystation/*")) == 1
    assert len(branches(other_repo, "waystation/*")) == 1


@pytest.mark.git
async def test_iteration_waits_for_submissions_until_the_queue_is_closed(
    host_repo: Path,
) -> None:
    async with queue() as runs:
        consumer = asyncio.create_task(_drain(runs))
        for _ in range(10):  # ample turns for an idle queue to end, if it would
            await asyncio.sleep(0)
        assert not consumer.done(), "an open queue with nothing in it ended"

        runs.submit(a_run(host_repo))
        runs.close()
        results = await consumer

    assert [type(result) for result in results] == [RunSucceeded], why(results)


@pytest.mark.git
async def test_a_failure_is_one_more_result_and_stops_no_other_run(
    host_repo: Path,
) -> None:
    failure_seen = asyncio.Event()

    def buggy(ctx: RunContext) -> None:
        raise RuntimeError("a bug in the flow script's hook")

    async with queue() as runs:
        runs.submit(a_run(host_repo).on_sandbox_ready(lambda ctx: failure_seen.wait()))
        runs.submit(a_run(host_repo).on_workspace_ready(buggy))
        runs.close()
        results: list[RunResult[Any]] = []
        async for result in runs:
            results.append(result)
            failure_seen.set()

    failed, succeeded = results
    assert isinstance(failed, RunFailed)
    assert isinstance(failed.failure, HookRaised)
    assert isinstance(succeeded, RunSucceeded), "the failure cancelled nothing"


@pytest.mark.git
async def test_max_concurrency_caps_everything_submitted_whenever_it_came(
    host_repo: Path,
) -> None:
    in_flight = peak = 0
    pair = asyncio.Barrier(2)  # so two runs are surely in flight together

    async def started(ctx: RunContext) -> None:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await pair.wait()

    def ended(ctx: RunContext, result: RunResult[Summary]) -> None:
        nonlocal in_flight
        in_flight -= 1

    spec = a_run(host_repo).on_run_start(started).on_run_end(ended)
    async with queue(max_concurrency=2) as runs:
        runs.submit(spec)
        runs.submit(spec)
        first = await anext(runs)
        runs.submit(spec)
        runs.submit(spec)
        runs.close()
        results = [first, *[result async for result in runs]]

    assert [type(result) for result in results] == [RunSucceeded] * 4, why(results)
    assert peak == 2


@pytest.mark.git
async def test_a_run_is_preflighted_when_it_starts_not_when_it_was_submitted(
    host_repo: Path, tmp_path: Path
) -> None:
    gate = Gate()
    prompt = tmp_path / "written-later.md"

    async with queue(max_concurrency=1) as runs:
        runs.submit(a_run(host_repo).on_run_start(lambda ctx: gate.hold()))
        runs.submit(a_run(host_repo, prompt))  # no such file yet
        consumer = asyncio.create_task(_drain(runs))
        await until(gate.reached.is_set, consumer)
        prompt.write_text(PROMPT)  # there by the time its slot frees
        gate.release.set()
        runs.close()
        results = await consumer

    assert [type(result) for result in results] == [RunSucceeded] * 2, why(results)


@pytest.mark.git
async def test_a_run_failing_preflight_reports_it_and_the_rest_go_on(
    host_repo: Path, tmp_path: Path, isolated_tempdir: Path
) -> None:
    missing = tmp_path / "never-written.md"

    async with queue() as runs:
        refused = runs.submit(a_run(host_repo, missing).name("ticket-7"))
        runs.submit(a_run(host_repo))
        runs.close()
        results = [result async for result in runs]

    failed = next(result for result in results if isinstance(result, RunFailed))
    assert failed.stage is None, "it never began, so no stage failed"
    assert failed.name == refused.spec.label == "ticket-7"
    assert str(missing) in str(failed.failure)
    assert [type(result) for result in results].count(RunSucceeded) == 1
    assert workspaces(isolated_tempdir) == []


@pytest.mark.git
async def test_leaving_the_block_stops_runs_in_flight_and_never_starts_queued(
    host_repo: Path, isolated_tempdir: Path
) -> None:
    stuck = Stuck(host_repo, count=2)
    queued: list[str] = []

    async with queue(max_concurrency=2) as runs:
        for spec in stuck.runs():
            runs.submit(spec)
        runs.submit(a_run(host_repo).on_run_start(lambda ctx: queued.append("x")))
        await until_batch(stuck.all_ready.is_set, runs)

    stuck.assert_work_kept(isolated_tempdir)
    assert queued == []
    # A stopped run reports nothing (ADR-0017), even to a late reader.
    assert [result async for result in runs] == []


@pytest.mark.git
async def test_cancelling_one_run_stops_it_alone_and_it_reports_nothing(
    host_repo: Path, isolated_tempdir: Path
) -> None:
    stuck = Stuck(host_repo, count=1)
    gate = Gate()

    async with queue() as runs:
        (spec,) = stuck.runs()
        stopped = runs.submit(spec)
        runs.submit(a_run(host_repo).on_sandbox_ready(lambda ctx: gate.hold()))
        await until_batch(
            lambda: stuck.all_ready.is_set() and gate.reached.is_set(), runs
        )

        await stopped.cancel()

        # Wound down by the time cancel returns: its work is kept already.
        (run_id,) = stuck.started
        assert subjects(host_repo, f"HEAD..waystation/{run_id}") == [
            "WIP: salvaged uncommitted work",
            "first",
        ]
        gate.release.set()
        runs.close()
        results = [result async for result in runs]

    assert [type(result) for result in results] == [RunSucceeded], why(results)
    assert workspaces(isolated_tempdir) == []


@pytest.mark.git
async def test_cancelling_a_queued_run_means_it_never_starts(host_repo: Path) -> None:
    gate = Gate()
    started: list[str] = []

    async with queue(max_concurrency=1) as runs:
        runs.submit(a_run(host_repo).on_sandbox_ready(lambda ctx: gate.hold()))
        waiting = runs.submit(
            a_run(host_repo).on_run_start(lambda ctx: started.append(ctx.run_id))
        )
        await until_batch(gate.reached.is_set, runs)
        await waiting.cancel()
        gate.release.set()
        runs.close()
        results = [result async for result in runs]

    assert started == []
    assert [type(result) for result in results] == [RunSucceeded], why(results)


@pytest.mark.git
async def test_cancelling_a_run_that_already_ended_leaves_its_result(
    host_repo: Path,
) -> None:
    async with queue() as runs:
        done = runs.submit(a_run(host_repo))
        runs.close()
        results = [result async for result in runs]
        await done.cancel()

    assert [type(result) for result in results] == [RunSucceeded], why(results)


@pytest.mark.git
async def test_consumers_sharing_a_queue_split_its_results_and_all_end_on_close(
    host_repo: Path,
) -> None:
    async with queue() as runs:
        runs.submit(a_run(host_repo))
        runs.submit(a_run(host_repo))
        first = asyncio.create_task(_drain(runs))
        second = asyncio.create_task(_drain(runs))
        third = asyncio.create_task(_drain(runs))
        runs.close()
        drained = await asyncio.gather(first, second, third)

    assert sum(len(results) for results in drained) == 2


@pytest.mark.git
async def test_a_wait_for_a_result_that_is_cancelled_loses_no_result(
    host_repo: Path,
) -> None:
    gate = Gate()
    async with queue() as runs:
        runs.submit(a_run(host_repo).on_sandbox_ready(lambda ctx: gate.hold()))
        runs.close()

        waiting = asyncio.create_task(anext(runs))
        await until(gate.reached.is_set, waiting)
        waiting.cancel()  # what asyncio.wait_for does when it gives up
        with pytest.raises(asyncio.CancelledError):
            await waiting
        gate.release.set()

        assert [type(result) async for result in runs] == [RunSucceeded]


@pytest.mark.git
async def test_a_consumer_cancelled_as_the_block_winds_down_waits_for_it(
    host_repo: Path, isolated_tempdir: Path
) -> None:
    gate = Gate()
    stuck = Stuck(host_repo, count=1, sandbox=GatedSandbox(gate, at="teardown"))

    async def consume() -> None:
        async with queue() as runs:
            for spec in stuck.runs():
                runs.submit(spec)
            await until_batch(stuck.all_ready.is_set, runs)

    consumer = asyncio.create_task(consume())
    await until(gate.reached.is_set, consumer)  # left; the run's teardown is held
    consumer.cancel()
    for _ in range(10):  # ample turns for a cancellation to end a task
        await asyncio.sleep(0)
    assert not consumer.done(), "the block was left with a run still winding down"

    gate.release.set()
    with pytest.raises(asyncio.CancelledError):
        await consumer
    stuck.assert_work_kept(isolated_tempdir)


@pytest.mark.git
async def test_a_closed_or_stopped_queue_refuses_a_submission(host_repo: Path) -> None:
    async with queue() as runs:
        runs.close()
        with pytest.raises(RuntimeError, match="closed"):
            runs.submit(a_run(host_repo))
    with pytest.raises(RuntimeError, match="closed"):
        runs.submit(a_run(host_repo))
    assert [result async for result in runs] == []


@pytest.mark.unit
@pytest.mark.parametrize("cap", [0, -1])
def test_a_cap_that_would_start_nothing_is_refused_at_the_call(cap: int) -> None:
    with pytest.raises(ValueError, match="max_concurrency"):
        queue(max_concurrency=cap)


async def _drain(runs: RunQueue[Any]) -> list[RunResult[Any]]:
    return [result async for result in runs]
