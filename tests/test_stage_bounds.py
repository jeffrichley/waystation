"""A stage's bound stops the stage's git, not just the wait for it (#57).

The workspace and integrate stages run host git. When their bound fires, that
git and everything it started is killed before the run reports ``TimedOut``
(ADR-0023), so nothing is still writing when the result says the stage
stopped. Only an ``update-ref`` already under way is left alone: the bound
waits for it, so the landing it makes is what the run reports (ADR-0027).
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from helpers import (
    a_run,
    awaited,
    git,
    stalling_ref_hook,
    subjects,
    until,
    workspaces,
)
from waystation import (
    GitRepo,
    Integration,
    IntegrationReport,
    PatchSeries,
    RunFailed,
    RunResult,
    RunSucceeded,
    TimedOut,
    Timeouts,
)
from waystation.clock import ManualClock, use_clock

TARGET = "feature"


def _beating(pulse: Path) -> str:
    """A shell loop that grows ``pulse`` for as long as it lives.

    Capped at 20 s so that, if a kill ever fails, the loop cannot outlive the
    test run; a working kill ends it long before.
    """
    beat = f"printf x >> '{pulse.as_posix()}'"
    return f"i=0; while [ $i -lt 400 ]; do {beat}; sleep 0.05; i=$((i+1)); done"


async def _fire_bound(
    clock: ManualClock, task: asyncio.Task[RunResult[Any]], started: Path
) -> RunResult[Any]:
    """Let the run reach its stalled git, then move the clock past the bound."""
    await until(started.exists, task)
    clock.advance(1.0)
    return await task


async def _still(pulse: Path) -> None:
    """Nothing is writing to ``pulse`` any more."""
    before = pulse.stat().st_size
    await asyncio.sleep(0.3)
    assert pulse.stat().st_size == before, "something is still running"


@dataclass(frozen=True)
class StallsBeforeCommitting(GitRepo):
    """A host whose first ``commit-tree`` waits on a git alias that never ends.

    The alias runs a shell under git, so killing git alone would leave that
    shell writing: the bound has to take the whole tree.
    """

    pulse: Path | None = None

    async def git(self, *args: str, env: Mapping[str, str] | None = None) -> str:
        assert self.pulse is not None
        if args[:1] == ("commit-tree",) and not self.pulse.exists():
            alias = f"alias.stall=!{_beating(self.pulse)}"
            await super().git("-c", alias, "stall")
        return await super().git(*args, env=env)


@dataclass(frozen=True)
class Stalling:
    """The shipped ``Integration``, landing through a host that stalls."""

    pulse: Path

    async def integrate(self, repo: GitRepo, series: PatchSeries) -> IntegrationReport:
        stalling = StallsBeforeCommitting(repo.path, repo.common_dir, pulse=self.pulse)
        return await Integration(TARGET).integrate(stalling, series)


@pytest.mark.git
async def test_an_integrate_bound_kills_the_landing_and_all_it_started(
    host_repo: Path, tmp_path: Path
) -> None:
    pulse = tmp_path / "pulse"
    spec = (
        a_run(host_repo)
        .integrate(Stalling(pulse))
        .with_timeouts(Timeouts(integrate=1.0))
    )
    clock = ManualClock()

    with use_clock(clock):
        result = await _fire_bound(clock, asyncio.create_task(awaited(spec)), pulse)

    assert isinstance(result, RunFailed)
    assert result.stage == "integrate"
    assert isinstance(result.failure, TimedOut)
    assert result.failure.bound == "integrate"
    assert git(host_repo, "branch", "--list", TARGET) == ""
    assert result.preserved == f"waystation/{result.run_id}"
    await _still(pulse)


@pytest.mark.git
async def test_a_bound_firing_mid_swap_lets_it_land_and_reports_the_landing(
    host_repo: Path, tmp_path: Path
) -> None:
    started, release = tmp_path / "started", tmp_path / "release"
    hooks = stalling_ref_hook(tmp_path / "hooks", started, release)
    git(host_repo, "config", "core.hooksPath", hooks.as_posix())
    spec = a_run(host_repo).integrate(TARGET).with_timeouts(Timeouts(integrate=1.0))
    clock = ManualClock()

    with use_clock(clock):
        task = asyncio.create_task(awaited(spec))
        await until(started.exists, task)
        clock.advance(1.0)  # the bound runs out while the swap is under way
        release.touch()
        result = await task

    assert isinstance(result, RunSucceeded)
    assert result.preserved is None
    assert subjects(host_repo, f"HEAD..{TARGET}") == ["add a file"]
    assert list((host_repo / ".git" / "refs" / "heads").glob("*.lock")) == []


@pytest.mark.git
async def test_a_workspace_bound_kills_the_clone_and_leaves_no_workspace(
    host_repo: Path,
    tmp_path: Path,
    isolated_tempdir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pulse = tmp_path / "pulse"
    hooks = tmp_path / "template" / "hooks"
    hooks.mkdir(parents=True)
    # Every clone copies its hooks from the template, so the workspace's own
    # checkout runs this one: a clone that stalls with a shell under it.
    hook = hooks / "post-checkout"
    hook.write_bytes(f"#!/bin/sh\n{_beating(pulse)}\n".encode())
    hook.chmod(0o755)
    monkeypatch.setenv("GIT_TEMPLATE_DIR", str(tmp_path / "template"))
    spec = a_run(host_repo).with_timeouts(Timeouts(workspace=1.0))
    clock = ManualClock()

    with use_clock(clock):
        result = await _fire_bound(clock, asyncio.create_task(awaited(spec)), pulse)

    assert isinstance(result, RunFailed)
    assert result.stage == "workspace"
    assert isinstance(result.failure, TimedOut)
    assert result.failure.bound == "workspace"
    assert workspaces(isolated_tempdir) == []
    await _still(pulse)
