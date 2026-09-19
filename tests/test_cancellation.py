"""Cancelling a run loses no work and leaks no sandbox (ADR-0017, ADR-0023).

Nothing reports a cancelled run: ``CancelledError`` reaches whoever awaited
it. What the agent left is still collected and kept on its preservation
branch, and the sandbox is still torn down, before the cancellation surfaces.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from helpers import (
    OK_OUTCOME,
    Gate,
    GatedSandbox,
    ShellAgent,
    a_run,
    awaited,
    branches,
    git,
    lifecycle,
    stalling_ref_hook,
    subjects,
    until,
    workspaces,
)
from waystation import (
    Flow,
    GitRepo,
    Integration,
    IntegrationReport,
    NoSandbox,
    PatchSeries,
    RunContext,
    RunResult,
    RunSpec,
    ScriptedAgent,
    ScriptedCommit,
    Summary,
)
from waystation.agents import AgentLine


@dataclass(frozen=True)
class GatedIntegration:
    """The shipped ``Integration``, held at the gate before it starts landing."""

    gate: Gate
    target: str

    async def integrate(self, repo: GitRepo, series: PatchSeries) -> IntegrationReport:
        await self.gate.hold()
        return await Integration(self.target).integrate(repo, series)


def _said(caplog: pytest.LogCaptureFixture, line: str) -> bool:
    return line in [record.getMessage() for record in lifecycle(caplog)]


async def _cancel_once_ready(
    task: asyncio.Task[RunResult[Any]], ready: asyncio.Event
) -> None:
    """Cancel ``task`` once ``ready`` is set; fail loudly if it ended first."""
    waiter = asyncio.create_task(ready.wait())
    await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
    waiter.cancel()
    if task.done():
        pytest.fail(f"the run ended before it was cancelled: {task.result()!r}")
    task.cancel()


async def _cancel_at(gate: Gate, spec: RunSpec[Any]) -> str:
    """Cancel a run of ``spec`` while it waits at ``gate``; return its run id."""
    run_ids: list[str] = []
    task = asyncio.create_task(
        awaited(spec.on_run_start(lambda ctx: run_ids.append(ctx.run_id)))
    )
    await _cancel_once_ready(task, gate.reached)
    gate.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    return run_ids[0]


@pytest.mark.git
async def test_cancelling_a_run_mid_agent_keeps_what_the_agent_left(
    host_repo: Path,
    isolated_tempdir: Path,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    pulse = tmp_path / "pulse"
    beat = f"printf x >> '{pulse.as_posix()}'"
    agent = ShellAgent(
        "\n".join(
            [
                "set -e",
                "printf 'a\\n' > a.txt",
                "git add a.txt",
                "git commit -q -m first",
                "printf 'wip\\n' > wip.txt",
                f"( while true; do {beat}; sleep 0.05; done ) &",
                "echo ready",
                "wait",
            ]
        )
    )
    ready = asyncio.Event()
    run_ids: list[str] = []
    ended: list[RunResult[Any]] = []

    def watch(ctx: RunContext, line: AgentLine) -> None:
        run_ids.append(ctx.run_id)
        if line.raw == "ready":
            ready.set()

    spec: RunSpec[Summary] = (
        Flow(host_repo, agent=agent, sandbox=NoSandbox())
        .run("work until stopped")
        .on_agent_output(watch)
        .on_run_end(lambda ctx, result: ended.append(result))
    )
    with caplog.at_level(logging.INFO, logger="waystation"):
        task = asyncio.create_task(awaited(spec))
        await _cancel_once_ready(task, ready)
        with pytest.raises(asyncio.CancelledError):
            await task

    branch = f"waystation/{run_ids[0]}"
    kept = subjects(host_repo, f"HEAD..{branch}")
    assert kept == ["WIP: salvaged uncommitted work", "first"]
    assert workspaces(isolated_tempdir) == []
    assert ended == []
    assert _said(caplog, f"run cancelled during agent; series kept on {branch}")
    # The agent's whole tree died with it: nothing is still writing.
    before = pulse.stat().st_size if pulse.exists() else 0
    await asyncio.sleep(0.2)
    assert (pulse.stat().st_size if pulse.exists() else 0) == before


@pytest.mark.git
async def test_a_cancellation_during_collect_waits_for_the_series_and_never_integrates(
    host_repo: Path, isolated_tempdir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    gate = Gate()
    spec = a_run(host_repo, sandbox=GatedSandbox(gate, at="collect"))

    with caplog.at_level(logging.INFO, logger="waystation"):
        run_id = await _cancel_at(gate, spec.integrate("feature"))

    branch = f"waystation/{run_id}"
    kept = subjects(host_repo, f"HEAD..{branch}")
    assert kept == ["add a file"]
    assert branches(host_repo, "feature") == []
    assert workspaces(isolated_tempdir) == []
    assert _said(caplog, f"run cancelled during collect; series kept on {branch}")


@pytest.mark.git
async def test_a_cancellation_during_integrate_lets_the_landing_finish(
    host_repo: Path, isolated_tempdir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    gate = Gate()
    spec = a_run(host_repo).integrate(GatedIntegration(gate, "feature"))

    with caplog.at_level(logging.INFO, logger="waystation"):
        await _cancel_at(gate, spec)

    landed = subjects(host_repo, "HEAD..feature")
    assert landed == ["add a file"]
    assert branches(host_repo, "waystation/*") == []
    assert workspaces(isolated_tempdir) == []
    assert _said(caplog, "run cancelled during integrate; series landed on feature")


@pytest.mark.git
async def test_a_cancellation_during_teardown_lets_the_teardown_finish(
    host_repo: Path, isolated_tempdir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    gate = Gate()
    spec = a_run(host_repo, sandbox=GatedSandbox(gate, at="teardown"))

    with caplog.at_level(logging.INFO, logger="waystation"):
        run_id = await _cancel_at(gate, spec.integrate("feature"))

    assert gate.passed.is_set()
    assert workspaces(isolated_tempdir) == []
    # Integrate had not started, so it never will: the series is kept instead.
    assert branches(host_repo, "feature") == []
    branch = f"waystation/{run_id}"
    assert branches(host_repo, "waystation/*") == [branch]
    assert _said(caplog, f"run cancelled during sandbox; series kept on {branch}")


@pytest.mark.git
async def test_a_cancellation_while_the_sandbox_starts_tears_it_down_unused(
    host_repo: Path, isolated_tempdir: Path
) -> None:
    gate = Gate()
    ready: list[str] = []
    spec = a_run(host_repo, sandbox=GatedSandbox(gate, at="start")).on_sandbox_ready(
        lambda ctx: ready.append(ctx.run_id)
    )

    await _cancel_at(gate, spec)

    assert gate.passed.is_set()
    assert ready == []
    assert workspaces(isolated_tempdir) == []
    assert branches(host_repo, "waystation/*") == []


@pytest.mark.git
async def test_a_run_cancelled_as_it_starts_leaves_nothing_behind(
    host_repo: Path, isolated_tempdir: Path
) -> None:
    def cancel_this_run(ctx: RunContext) -> None:
        task = asyncio.current_task()
        assert task is not None
        task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await a_run(host_repo).on_run_start(cancel_this_run)

    assert workspaces(isolated_tempdir) == []
    assert branches(host_repo, "waystation/*") == []


@pytest.mark.git
async def test_a_failure_a_cancelled_run_can_no_longer_report_is_logged(
    host_repo: Path, caplog: pytest.LogCaptureFixture
) -> None:
    gate = Gate()
    agent = ScriptedAgent(
        outcome=OK_OUTCOME,
        exit_code=3,
        commits=(ScriptedCommit("add a file", {"a.txt": "x"}),),
    )
    spec: RunSpec[Summary] = Flow(
        host_repo, agent=agent, sandbox=GatedSandbox(gate, at="collect")
    ).run("fail, then be cancelled")

    with caplog.at_level(logging.INFO, logger="waystation"):
        run_id = await _cancel_at(gate, spec)

    unreported = [
        r.getMessage()
        for r in caplog.records
        if r.name == "waystation" and r.levelno == logging.ERROR
    ]
    assert len(unreported) == 1
    assert unreported[0].startswith(
        f"run {run_id}: agent failed in a run that was cancelled: AgentExited("
    )
    assert branches(host_repo, "waystation/*") == [f"waystation/{run_id}"]


@pytest.mark.git
async def test_a_landing_refused_under_cancellation_keeps_the_series(
    host_repo: Path, caplog: pytest.LogCaptureFixture
) -> None:
    gate = Gate()
    checked_out = git(host_repo, "symbolic-ref", "--short", "HEAD")
    tip = git(host_repo, "rev-parse", checked_out)
    spec = a_run(host_repo).integrate(GatedIntegration(gate, checked_out))

    with caplog.at_level(logging.INFO, logger="waystation"):
        run_id = await _cancel_at(gate, spec)

    assert git(host_repo, "rev-parse", checked_out) == tip
    branch = f"waystation/{run_id}"
    assert subjects(host_repo, f"HEAD..{branch}") == ["add a file"]
    assert _said(caplog, f"run cancelled during integrate; series kept on {branch}")


@pytest.mark.git
async def test_a_cancellation_inside_a_hook_is_still_said(
    host_repo: Path, isolated_tempdir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    gate = Gate()
    spec = a_run(host_repo).on_sandbox_ready(lambda ctx: gate.hold())

    with caplog.at_level(logging.INFO, logger="waystation"):
        await _cancel_at(gate, spec)

    # Hooks are the user's code: a cancellation interrupts them.
    assert not gate.passed.is_set()
    assert workspaces(isolated_tempdir) == []
    assert _said(caplog, "run cancelled during sandbox")


@pytest.mark.git
async def test_a_cancellation_during_preservation_waits_for_it_then_surfaces(
    host_repo: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    started, release = tmp_path / "started", tmp_path / "release"
    hooks = stalling_ref_hook(tmp_path / "hooks", started, release)
    git(host_repo, "config", "core.hooksPath", hooks.as_posix())
    run_ids: list[str] = []
    spec = a_run(host_repo).on_run_start(lambda ctx: run_ids.append(ctx.run_id))

    with caplog.at_level(logging.INFO, logger="waystation"):
        task = asyncio.create_task(awaited(spec))
        await until(started.exists, task)  # the preservation branch is landing
        task.cancel()
        release.touch()
        with pytest.raises(asyncio.CancelledError):
            await task

    branch = f"waystation/{run_ids[0]}"
    assert subjects(host_repo, f"HEAD..{branch}") == ["add a file"]
    assert _said(caplog, f"run cancelled during integrate; series kept on {branch}")
