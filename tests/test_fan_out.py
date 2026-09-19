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
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from helpers import (
    OK_OUTCOME,
    PROMPT,
    ShellAgent,
    a_run,
    git,
    host_state,
    init_host_repo,
    subjects,
    until,
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
    ScriptedAgent,
    Summary,
    fan_out,
)
from waystation.agents import AgentLine


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


def _kept(repo: Path) -> list[str]:
    """The preservation branches in ``repo``."""
    listed = git(repo, "branch", "--list", "--format=%(refname:short)", "waystation/*")
    return listed.splitlines()


# Commits, leaves work uncommitted, says so, then works until it is stopped.
_WORKS_UNTIL_STOPPED = ShellAgent(
    "\n".join(
        [
            "set -e",
            "printf 'a\\n' > a.txt",
            "git add a.txt",
            "git commit -q -m first",
            "printf 'wip\\n' > wip.txt",
            "echo ready",
            "while true; do sleep 0.05; done",
        ]
    )
)


@dataclass
class Stuck:
    """Runs that work until stopped; ``all_ready`` once ``count`` are working."""

    repo: Path
    count: int
    started: list[str] = field(default_factory=list)
    working: set[str] = field(default_factory=set)
    all_ready: asyncio.Event = field(default_factory=asyncio.Event)

    def runs(self) -> list[RunSpec[Summary]]:
        spec: RunSpec[Summary] = (
            Flow(self.repo, agent=_WORKS_UNTIL_STOPPED, sandbox=NoSandbox())
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
        branches = [f"waystation/{run_id}" for run_id in self.started]
        assert sorted(_kept(self.repo)) == sorted(branches)
        for branch in branches:
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

    assert [type(result) for result in results] == [RunSucceeded, RunSucceeded]
    assert len(_kept(host_repo)) == len(_kept(other_repo)) == 1
    assert sorted(_kept(host_repo) + _kept(other_repo)) == sorted(
        str(result.preserved) for result in results
    )


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

    assert [type(result) for result in results] == [RunSucceeded] * 3


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

    assert [type(result) for result in results] == [RunSucceeded] * 4
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

    async with fan_out([*stuck.runs(), never], max_concurrency=2):
        await stuck.all_ready.wait()

    # By the time the block is left, every run it stopped has wound down.
    assert len(stuck.started) == 2
    stuck.assert_work_kept(isolated_tempdir)
    assert queued == []


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

    assert [type(result) for result in results] == [RunSucceeded] * 4
    assert sorted(checked) == ["agent a", "agent b", "sandbox x", "sandbox y"]


@pytest.mark.git
async def test_a_problem_anywhere_in_the_batch_stops_it_before_any_run_starts(
    host_repo: Path, tmp_path: Path, isolated_tempdir: Path
) -> None:
    started: list[str] = []
    missing = tmp_path / "prompts" / "never-written.md"

    def batch() -> Iterator[RunSpec[Summary]]:
        # Read to its end before anything starts, so the last spec counts too.
        yield a_run(host_repo).on_run_start(lambda ctx: started.append(ctx.run_id))
        yield a_run(host_repo, missing)

    before = host_state(host_repo)
    with pytest.raises(PreflightError) as refused:
        async for _ in fan_out(batch()):
            pytest.fail("a batch that failed preflight yielded a result")

    assert str(missing) in str(refused.value)
    assert started == []
    assert workspaces(isolated_tempdir) == []
    assert host_state(host_repo) == before
