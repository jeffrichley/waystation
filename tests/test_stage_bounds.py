"""A stage's bound stops the stage's git, not just the wait for it (#57).

The workspace and integrate stages run host git. When their bound fires, that
git and everything it started is killed before the run reports ``TimedOut``
(ADR-0023), so nothing is still writing when the result says the stage
stopped. Only the compare-and-swap that moves a target is left to finish: once
it has started, the landing it makes is what the run reports.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from helpers import a_run, awaited, git, subjects, workspaces
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
    """A shell loop that grows ``pulse`` for as long as it lives — 20 s at most."""
    beat = f"printf x >> '{pulse.as_posix()}'"
    return f"i=0; while [ $i -lt 400 ]; do {beat}; sleep 0.05; i=$((i+1)); done"


async def _when(ready: Callable[[], bool], task: asyncio.Task[Any]) -> None:
    """Wait until ``ready()`` — the stage is mid-git — failing if the run ends."""
    while not ready():
        if task.done():
            pytest.fail(f"the run ended first: {task.result()!r}")
        await asyncio.sleep(0.02)


async def _fire_bound(
    clock: ManualClock, task: asyncio.Task[RunResult[Any]], started: Path
) -> RunResult[Any]:
    """Let the run reach its stalled git, then move the clock past the bound."""
    await _when(started.exists, task)
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


def _stalling_ref_hook(hooks: Path, started: Path, release: Path) -> None:
    """A reference-transaction hook: the first swap waits for ``release``.

    It runs inside ``update-ref`` while git holds the ref's lock, which is the
    one moment a kill would leave a lock behind.
    """
    hooks.mkdir()
    hook = hooks / "reference-transaction"
    hook.write_bytes(
        "\n".join(
            [
                "#!/bin/sh",
                "cat > /dev/null",
                f"if [ \"$1\" = prepared ] && [ ! -f '{started.as_posix()}' ]; then",
                f"  : > '{started.as_posix()}'",
                "  i=0",
                f"  while [ ! -f '{release.as_posix()}' ] && [ $i -lt 400 ]; do",
                "    sleep 0.05; i=$((i+1))",
                "  done",
                "fi",
                "",
            ]
        ).encode()
    )
    hook.chmod(0o755)


@pytest.mark.git
async def test_a_bound_firing_mid_swap_lets_it_land_and_reports_the_landing(
    host_repo: Path, tmp_path: Path
) -> None:
    started, release = tmp_path / "started", tmp_path / "release"
    _stalling_ref_hook(tmp_path / "hooks", started, release)
    git(host_repo, "config", "core.hooksPath", (tmp_path / "hooks").as_posix())
    spec = a_run(host_repo).integrate(TARGET).with_timeouts(Timeouts(integrate=1.0))
    clock = ManualClock()

    with use_clock(clock):
        task = asyncio.create_task(awaited(spec))
        await _when(started.exists, task)
        clock.advance(1.0)
        for _ in range(10):  # let the bound's cancellation reach the swap
            await asyncio.sleep(0)
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
