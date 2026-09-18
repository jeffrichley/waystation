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

from helpers import a_run, awaited, git
from waystation import (
    GitRepo,
    Integration,
    IntegrationReport,
    PatchSeries,
    RunFailed,
    RunResult,
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
