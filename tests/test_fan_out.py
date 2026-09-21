"""A batch of runs, run concurrently, each result yielded as it completes (#31).

``fan_out`` reads the whole batch, preflights it once, and yields every run's
typed result in the order the runs finish. A run that fails is one more
result; nothing raises mid-iteration and nothing is cancelled for it
(ADR-0007). Leaving ``async with fan_out(batch) as results`` early is the
one way to stop a batch: it cancels the runs in flight, which still keep
their work and tear down (ADR-0017), and never starts the queued ones.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from helpers import (
    OK_OUTCOME,
    PROMPT,
    WORKS_UNTIL_STOPPED,
    Gate,
    GatedSandbox,
    ShellAgent,
    a_run,
    branches,
    host_state,
    init_host_repo,
    subjects,
    until,
    until_batch,
    workspaces,
)
from waystation import (
    Flow,
    HookRaised,
    NoSandbox,
    PreflightError,
    RunContext,
    RunFailed,
    RunResult,
    RunSpec,
    RunSucceeded,
    SandboxBackend,
    ScriptedAgent,
    Summary,
    fan_out,
)
from waystation.agents import AgentLine


def _why(results: Sequence[RunResult[Any]]) -> list[str]:
    """Each failed run's stage and failure, so an assertion says what broke."""
    return [
        f"{result.stage}: {result.failure!r}"
        for result in results
        if isinstance(result, RunFailed)
    ]


@dataclass(frozen=True)
class CheckedAgent(ScriptedAgent):
    """A scripted agent that notes each preflight it is asked for."""

    label: str = ""
    checked: list[str] = field(default_factory=list, compare=False)

    def preflight(self) -> None:
        self.checked.append(f"agent {self.label}")


@dataclass(frozen=True)
class CheckedSandbox(NoSandbox):
    """``NoSandbox``, noting each preflight it is asked for."""

    label: str = ""
    checked: list[str] = field(default_factory=list, compare=False)

    async def preflight(self) -> None:
        self.checked.append(f"sandbox {self.label}")


@dataclass
class Stuck:
    """Runs that work until stopped; ``all_ready`` once ``count`` are working."""

    repo: Path
    count: int
    sandbox: SandboxBackend = field(default_factory=NoSandbox)
    started: list[str] = field(default_factory=list)
    working: set[str] = field(default_factory=set)
    all_ready: asyncio.Event = field(default_factory=asyncio.Event)

    def runs(self) -> list[RunSpec[Summary]]:
        spec: RunSpec[Summary] = (
            Flow(self.repo, agent=ShellAgent(WORKS_UNTIL_STOPPED), sandbox=self.sandbox)
            .run("work until stopped")
            .on_run_start(lambda ctx: self.started.append(ctx.run_id))
            .on_agent_output(self._watch)
        )
        return [spec] * self.count

    def _watch(self, ctx: RunContext, line: AgentLine) -> None:
        if line.raw == "ready":
            self.working.add(ctx.run_id)
            if len(self.working) == self.count:
                self.all_ready.set()

    def assert_work_kept(self, temp: Path) -> None:
        """Each run was stopped, its work kept on its branch, its sandbox gone."""
        kept = [f"waystation/{run_id}" for run_id in self.started]
        assert sorted(branches(self.repo, "waystation/*")) == sorted(kept)
        for branch in kept:
            assert subjects(self.repo, f"HEAD..{branch}") == [
                "WIP: salvaged uncommitted work",
                "first",
            ]
        assert workspaces(temp) == []


@pytest.mark.git
async def test_a_batch_across_host_repos_yields_every_run_result(
    host_repo: Path, tmp_path: Path
) -> None:
    (tmp_path / "other").mkdir()
    other_repo = init_host_repo(tmp_path / "other")
    batch = [a_run(host_repo), a_run(other_repo)]

    results: list[RunResult[Any]] = [result async for result in fan_out(batch)]

    assert [type(result) for result in results] == [RunSucceeded] * 2, _why(results)
    here, there = (branches(repo, "waystation/*") for repo in (host_repo, other_repo))
    assert len(here) == len(there) == 1, "each run kept its work in its own repo"
    assert sorted(here + there) == sorted(str(result.preserved) for result in results)


@pytest.mark.git
async def test_results_arrive_as_runs_finish_and_a_failure_stops_no_other_run(
    host_repo: Path,
) -> None:
    failure_seen = asyncio.Event()

    def buggy(ctx: RunContext) -> None:
        raise RuntimeError("a bug in the flow script's hook")

    # First in the batch, but held until the consumer has the failure in hand.
    slow = a_run(host_repo).on_sandbox_ready(lambda ctx: failure_seen.wait())
    broken = a_run(host_repo).on_workspace_ready(buggy)

    results: list[RunResult[Any]] = []
    async for result in fan_out([slow, broken]):
        results.append(result)
        failure_seen.set()

    failed, succeeded = results
    assert isinstance(failed, RunFailed)
    assert isinstance(failed.failure, HookRaised)
    assert isinstance(succeeded, RunSucceeded), "the failure cancelled nothing"


@pytest.mark.git
async def test_every_run_is_in_flight_at_once_by_default(host_repo: Path) -> None:
    together = asyncio.Barrier(3)  # passes only once all three have started
    batch = [a_run(host_repo).on_run_start(lambda ctx: together.wait())] * 3

    results = [result async for result in fan_out(batch)]

    assert [type(result) for result in results] == [RunSucceeded] * 3, _why(results)


@pytest.mark.git
async def test_max_concurrency_caps_the_runs_in_flight(host_repo: Path) -> None:
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

    batch = [a_run(host_repo).on_run_start(started).on_run_end(ended)] * 4

    results = [result async for result in fan_out(batch, max_concurrency=2)]

    assert [type(result) for result in results] == [RunSucceeded] * 4, _why(results)
    assert peak == 2


@pytest.mark.git
async def test_a_queued_run_resolves_its_base_when_its_workspace_stage_starts(
    host_repo: Path,
) -> None:
    lands = a_run(host_repo).integrate("batch")
    # "batch" does not exist yet: only the first run's landing makes it.
    follows: RunSpec[Summary] = Flow(
        host_repo,
        agent=ScriptedAgent(outcome=OK_OUTCOME),
        sandbox=NoSandbox(),
        base="batch",
    ).run(PROMPT)

    first, second = [r async for r in fan_out([lands, follows], max_concurrency=1)]

    assert isinstance(first, RunSucceeded)
    assert first.report is not None
    assert isinstance(second, RunSucceeded)
    assert second.base_sha == first.report.target_after


@pytest.mark.git
async def test_consumers_sharing_one_fan_out_split_its_results_each_run_once(
    host_repo: Path,
) -> None:
    started: list[str] = []
    batch = [a_run(host_repo).on_run_start(lambda ctx: started.append(ctx.run_id))] * 2
    results = fan_out(batch)

    async def drain() -> list[RunResult[Summary]]:
        return [result async for result in results]

    first, second = await asyncio.gather(drain(), drain())

    assert len(first) + len(second) == 2
    assert len(started) == 2


@pytest.mark.git
async def test_a_wait_for_a_result_that_is_cancelled_loses_no_result(
    host_repo: Path,
) -> None:
    gate = Gate()
    results = fan_out([a_run(host_repo).on_sandbox_ready(lambda ctx: gate.hold())])

    waiting = asyncio.create_task(anext(results))
    await until(gate.reached.is_set, waiting)
    waiting.cancel()  # what asyncio.wait_for does when it gives up
    with pytest.raises(asyncio.CancelledError):
        await waiting
    gate.release.set()

    assert [type(result) async for result in results] == [RunSucceeded]


@pytest.mark.unit
@pytest.mark.parametrize("cap", [0, -1])
def test_a_cap_that_would_start_nothing_is_refused_at_the_call(cap: int) -> None:
    with pytest.raises(ValueError, match="max_concurrency"):
        fan_out([], max_concurrency=cap)


@pytest.mark.git
async def test_leaving_the_block_early_stops_runs_in_flight_and_never_starts_queued(
    host_repo: Path, isolated_tempdir: Path
) -> None:
    stuck = Stuck(host_repo, count=2)
    queued: list[str] = []
    never = a_run(host_repo).on_run_start(lambda ctx: queued.append(ctx.run_id))

    async with fan_out([*stuck.runs(), never], max_concurrency=2) as results:
        await until_batch(stuck.all_ready.is_set, results)

    # By the time the block is left, every run it stopped has wound down.
    assert len(stuck.started) == 2
    stuck.assert_work_kept(isolated_tempdir)
    assert queued == []
    # A stopped run reports nothing (ADR-0017), even to a late reader.
    assert [result async for result in results] == []


@pytest.mark.git
async def test_a_consumer_cancelled_in_the_block_stops_its_runs_before_it_ends(
    host_repo: Path, isolated_tempdir: Path
) -> None:
    stuck = Stuck(host_repo, count=2)

    async def consume() -> None:
        async with fan_out(stuck.runs()) as results:
            async for result in results:
                pytest.fail(f"a run that works until stopped ended: {result!r}")

    consumer = asyncio.create_task(consume())
    await until(stuck.all_ready.is_set, consumer)
    consumer.cancel()  # what Ctrl-C does to the task awaiting the batch
    with pytest.raises(asyncio.CancelledError):
        await consumer

    stuck.assert_work_kept(isolated_tempdir)


@pytest.mark.git
async def test_a_consumer_cancelled_as_the_block_winds_down_waits_for_it(
    host_repo: Path, isolated_tempdir: Path
) -> None:
    gate = Gate()
    stuck = Stuck(host_repo, count=1, sandbox=GatedSandbox(gate, at="teardown"))

    async def consume() -> None:
        async with fan_out(stuck.runs()) as results:
            await until_batch(stuck.all_ready.is_set, results)

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
async def test_an_empty_batch_yields_nothing_in_either_form() -> None:
    nothing: list[RunSpec[Summary]] = []

    assert [result async for result in fan_out(nothing)] == []
    async with fan_out(nothing) as results:
        assert [result async for result in results] == []


@pytest.mark.git
def test_ctrl_c_on_a_bare_iteration_still_keeps_every_runs_work(
    host_repo: Path, isolated_tempdir: Path
) -> None:
    """A bare ``async for`` has no block to leave: ``asyncio.run`` stops the rest."""
    stuck = Stuck(host_repo, count=2)

    async def flow_script() -> None:
        consumer = asyncio.current_task()
        assert consumer is not None

        async def ctrl_c() -> None:
            await stuck.all_ready.wait()
            consumer.cancel()  # what asyncio.run does with the first Ctrl-C

        pressed = asyncio.create_task(ctrl_c())
        async for result in fan_out(stuck.runs()):
            pytest.fail(f"a run that works until stopped ended: {result!r}")
        pressed.cancel()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(flow_script())

    stuck.assert_work_kept(isolated_tempdir)


@pytest.mark.git
async def test_each_distinct_agent_and_sandbox_spec_is_preflighted_once(
    host_repo: Path,
) -> None:
    checked: list[str] = []

    def run(agent: str, sandbox: str) -> RunSpec[Summary]:
        # A fresh but equal value each time: specs dedupe by value, not identity.
        return Flow(
            host_repo,
            agent=CheckedAgent(outcome=OK_OUTCOME, label=agent, checked=checked),
            sandbox=CheckedSandbox(label=sandbox, checked=checked),
        ).run(PROMPT)

    batch = [run("a", "x"), run("a", "x"), run("b", "x"), run("a", "y")]

    results = [result async for result in fan_out(batch)]

    assert [type(result) for result in results] == [RunSucceeded] * 4, _why(results)
    assert sorted(checked) == ["agent a", "agent b", "sandbox x", "sandbox y"]


async def _iterated(batch: Iterable[RunSpec[Summary]]) -> None:
    async for _ in fan_out(batch):
        pytest.fail("a batch that failed preflight yielded a result")


async def _entered(batch: Iterable[RunSpec[Summary]]) -> None:
    async with fan_out(batch):
        pytest.fail("a batch that failed preflight entered its block")


@pytest.mark.git
@pytest.mark.parametrize("consume", [_iterated, _entered], ids=["for", "with"])
async def test_a_problem_anywhere_in_the_batch_stops_it_before_any_run_starts(
    host_repo: Path,
    tmp_path: Path,
    isolated_tempdir: Path,
    consume: Callable[[Iterable[RunSpec[Summary]]], Awaitable[None]],
) -> None:
    started: list[str] = []
    missing = tmp_path / "prompts" / "never-written.md"

    def batch() -> Iterator[RunSpec[Summary]]:
        # Read to its end before anything starts, so the last spec counts too.
        yield a_run(host_repo).on_run_start(lambda ctx: started.append(ctx.run_id))
        yield a_run(host_repo, missing)

    before = host_state(host_repo)
    with pytest.raises(PreflightError) as refused:
        await consume(batch())

    assert str(missing) in str(refused.value)
    assert started == []
    assert workspaces(isolated_tempdir) == []
    assert host_state(host_repo) == before


@pytest.mark.git
async def test_waiting_inside_a_batch_ends_when_a_run_does_rather_than_hanging(
    host_repo: Path,
) -> None:
    """A run that ends before the point being waited for must not wedge the wait.

    A failure is a value (ADR-0016): a run whose agent dies before the point
    a test is waiting for completes quietly, so a bare wait on that point
    never returns and the whole batch waits with it. On Windows CI that hung
    a worker past its timeout, which pytest-timeout ends with ``os._exit``
    — a crash with no traceback for the next reader (#105).
    """
    working: set[str] = set()

    def watch(ctx: RunContext, line: AgentLine) -> None:
        if line.raw == "ready":
            working.add(ctx.run_id)

    works: RunSpec[Summary] = (
        Flow(host_repo, agent=ShellAgent(WORKS_UNTIL_STOPPED), sandbox=NoSandbox())
        .run("work until stopped")
        .on_agent_output(watch)
    )
    # The other run never reaches `ready` — what a git call failing under
    # `set -e` does to WORKS_UNTIL_STOPPED on a loaded host.
    dies: RunSpec[Summary] = (
        Flow(host_repo, agent=ShellAgent("exit 3"), sandbox=NoSandbox())
        .run("end before saying anything")
        .on_agent_output(watch)
    )

    with pytest.raises(pytest.fail.Exception, match="ended"):
        # The bound is the regression's shape, not a behaviour under test: a
        # wait that wedges hangs forever, and this says so instead. Generous
        # against the two runs it starts on a loaded runner — a bound that
        # fires early here would be one more flake, which is the whole point.
        async with asyncio.timeout(40):
            async with fan_out([works, dies]) as results:
                await until_batch(lambda: len(working) == 2, results)
