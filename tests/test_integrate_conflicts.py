"""Conflicts and named-target refusals, through the public run API (issue #24)."""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import pytest

from helpers import a_run, commit_on, git, lifecycle, subjects
from waystation import (
    CommandFailed,
    Integration,
    PatchSeries,
    Refused,
    RunConflicted,
    RunFailed,
    RunSucceeded,
    ScriptedCommit,
    Summary,
)
from waystation.integration import (
    Conflict,
    FailedPatch,
    GitRepo,
    IntegrationReport,
    IntegrationStrategy,
)

pytestmark = pytest.mark.git

TARGET = "agents/batch"


# Patch 0 lands cleanly; patch 1 adds the file the target already added.
CLAIMS_SHARED = (
    ScriptedCommit("add notes", {"notes.txt": "n\n"}),
    ScriptedCommit("claim shared", {"shared.txt": "agent\n"}),
)


async def test_an_apply_conflict_is_a_conflicted_result_naming_the_patch(
    host_repo: Path,
) -> None:
    base = git(host_repo, "rev-parse", "HEAD")
    tip = commit_on(host_repo, TARGET, {"shared.txt": "target\n"})

    result = await a_run(host_repo, commits=CLAIMS_SHARED).integrate(TARGET)

    assert isinstance(result, RunConflicted)
    assert result.outcome == Summary(summary="ok"), "the Outcome is kept"
    report = result.report
    assert (report.strategy, report.target, report.mechanism) == (
        "Integration",
        TARGET,
        "apply",
    )
    assert report.conflict == Conflict(
        paths=("shared.txt",),
        failed_patch=FailedPatch(index=1, subject="claim shared"),
    )
    assert report.target_before == tip
    assert report.target_after is None
    assert report.landed == ()
    assert git(host_repo, "rev-parse", TARGET) == tip, "nothing landed"
    assert result.preserved == f"waystation/{result.run_id}"
    preserved = subjects(host_repo, f"{base}..{result.preserved}")
    assert preserved == ["claim shared", "add notes"]


async def test_a_merge_conflict_names_the_paths_but_no_patch(host_repo: Path) -> None:
    tip = commit_on(host_repo, TARGET, {"shared.txt": "target\n"})

    result = await a_run(host_repo, commits=CLAIMS_SHARED).integrate(
        TARGET, mechanism="merge"
    )

    assert isinstance(result, RunConflicted)
    assert result.report.mechanism == "merge"
    assert result.report.conflict == Conflict(paths=("shared.txt",), failed_patch=None)
    assert (result.report.target_before, result.report.target_after) == (tip, None)
    assert result.report.landed == ()
    assert git(host_repo, "rev-parse", TARGET) == tip
    assert result.preserved == f"waystation/{result.run_id}"


def host_state(repo: Path) -> dict[str, object]:
    """What a landing must never touch: refs, HEAD, the index and the tree."""
    return {
        "refs": git(repo, "for-each-ref", "--format=%(refname) %(objectname)"),
        "head": git(repo, "symbolic-ref", "HEAD"),
        "index": (repo / ".git" / "index").read_bytes(),
        "tree": {
            path.relative_to(repo).as_posix(): path.read_bytes()
            for path in repo.rglob("*")
            if path.is_file() and path.relative_to(repo).parts[0] != ".git"
        },
    }


@pytest.mark.parametrize("mechanism", ["apply", "merge"])
async def test_a_conflict_leaves_the_host_as_it_was(
    host_repo: Path, mechanism: Literal["apply", "merge"]
) -> None:
    commit_on(host_repo, TARGET, {"shared.txt": "target\n"})
    # A host mid-edit, so an index or tree that was touched can't look clean.
    (host_repo / "staged.txt").write_bytes(b"staged\n")
    git(host_repo, "add", "staged.txt")
    (host_repo / "README").write_bytes(b"edited, not staged\n")
    (host_repo / "scratch.log").write_bytes(b"untracked\n")
    before = host_state(host_repo)

    result = await a_run(host_repo, commits=CLAIMS_SHARED).integrate(
        TARGET, mechanism=mechanism
    )

    assert isinstance(result, RunConflicted)
    after = host_state(host_repo)
    preserved = f"refs/heads/{result.preserved} "
    after["refs"] = "\n".join(
        line
        for line in str(after["refs"]).splitlines()
        if not line.startswith(preserved)
    )
    assert after == before


# Claims shared.txt, then gives it back: patch by patch the first commit
# conflicts, but merged as a whole the series changes nothing there.
GIVES_BACK = (
    ScriptedCommit("claim shared", {"shared.txt": "agent\n"}),
    ScriptedCommit("give it back", {"shared.txt": "base\n"}),
)


async def test_an_apply_conflict_never_falls_back_to_merge(host_repo: Path) -> None:
    home = git(host_repo, "symbolic-ref", "--short", "HEAD")
    commit_on(host_repo, home, {"shared.txt": "base\n"})
    tip = commit_on(host_repo, TARGET, {"shared.txt": "target\n"})

    applied = await a_run(host_repo, commits=GIVES_BACK).integrate(TARGET)

    assert isinstance(applied, RunConflicted)
    assert applied.report.mechanism == "apply"
    assert applied.report.conflict is not None
    assert applied.report.conflict.failed_patch == FailedPatch(0, "claim shared")
    assert git(host_repo, "rev-parse", TARGET) == tip
    merged = await a_run(host_repo, commits=GIVES_BACK).integrate(
        TARGET, mechanism="merge"
    )
    assert isinstance(merged, RunSucceeded), "as one merge it lands cleanly"


async def test_a_conflict_is_logged_as_one_and_names_the_kept_branch(
    host_repo: Path, caplog: pytest.LogCaptureFixture
) -> None:
    commit_on(host_repo, TARGET, {"shared.txt": "target\n"})

    with caplog.at_level(logging.INFO, logger="waystation"):
        result = await a_run(host_repo, commits=CLAIMS_SHARED).integrate(TARGET)

    assert isinstance(result, RunConflicted)
    lines = lifecycle(caplog)
    events = [record.getMessage().split(":")[0] for record in lines]
    assert "integrated" not in events
    assert lines[-1].levelno == logging.INFO
    end = lines[-1].getMessage()
    assert end.startswith("run end: conflicted")
    assert TARGET in end
    assert f"{result.preserved}" in end


def assert_refused(result: object, reason: str, repo: Path) -> None:
    """Failed at integrate for ``reason``, with the series kept all the same."""
    assert isinstance(result, RunFailed)
    assert result.stage == "integrate"
    assert isinstance(result.failure, Refused)
    assert result.failure.reason == reason
    assert result.preserved == f"waystation/{result.run_id}"
    assert git(repo, "log", "-1", "--format=%s", result.preserved) == "add a file"


async def test_a_target_checked_out_in_the_main_worktree_is_refused(
    host_repo: Path,
) -> None:
    home = git(host_repo, "symbolic-ref", "--short", "HEAD")
    tip = git(host_repo, "rev-parse", home)

    result = await a_run(host_repo).integrate(home)

    assert_refused(result, "target_checked_out", host_repo)
    assert git(host_repo, "rev-parse", home) == tip
    assert git(host_repo, "status", "--porcelain") == ""


async def test_a_checked_out_target_is_refused_even_when_nothing_would_land(
    host_repo: Path,
) -> None:
    # The target is a setup mistake whatever the agent did, as `git branch -f`
    # refuses a no-op; a batch shouldn't pass or fail on who happened to commit.
    home = git(host_repo, "symbolic-ref", "--short", "HEAD")

    result = await a_run(host_repo, commits=()).integrate(home)

    assert isinstance(result, RunFailed)
    assert result.stage == "integrate"
    assert isinstance(result.failure, Refused)
    assert result.failure.reason == "target_checked_out"
    assert result.preserved is None, "an empty series has nothing to keep"


async def test_a_target_checked_out_in_another_worktree_is_refused(
    host_repo: Path, tmp_path: Path
) -> None:
    git(host_repo, "worktree", "add", "-b", TARGET, str(tmp_path / "elsewhere"))
    tip = git(host_repo, "rev-parse", TARGET)

    result = await a_run(host_repo).integrate(TARGET)

    assert_refused(result, "target_checked_out", host_repo)
    assert git(host_repo, "rev-parse", TARGET) == tip


def rebasing(worktree: Path) -> None:
    """Stop a rebase of the worktree's branch halfway, HEAD detached from it."""
    stopped = subprocess.run(
        ["git", "rebase", "--no-ff", "--exec", "false", "HEAD~1"],
        cwd=worktree,
        capture_output=True,
        check=False,
    )
    assert stopped.returncode != 0, "the exec stops the rebase"


def bisecting(worktree: Path) -> None:
    """Start a bisect from the worktree's branch, then leave it for a midpoint."""
    git(worktree, "bisect", "start")
    git(worktree, "checkout", "--detach")


@pytest.mark.parametrize("stop", [rebasing, bisecting], ids=["rebase", "bisect"])
async def test_a_target_a_worktree_will_return_to_is_refused(
    host_repo: Path, tmp_path: Path, stop: Callable[[Path], None]
) -> None:
    # Detached, but the branch is still that worktree's: finishing the rebase
    # would write over whatever landed, as `git branch -f` knows (ADR-0020).
    elsewhere = tmp_path / "elsewhere"
    git(host_repo, "worktree", "add", "-b", TARGET, str(elsewhere))
    (elsewhere / "theirs.txt").write_bytes(b"theirs\n")
    git(elsewhere, "add", "theirs.txt")
    git(elsewhere, "commit", "-m", "theirs")
    stop(elsewhere)
    assert git(elsewhere, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"
    tip = git(host_repo, "rev-parse", f"refs/heads/{TARGET}")

    result = await a_run(host_repo).integrate(TARGET)

    assert_refused(result, "target_checked_out", host_repo)
    assert git(host_repo, "rev-parse", f"refs/heads/{TARGET}") == tip


@dataclass(frozen=True)
class MovesOnSwap(GitRepo):
    """A host whose target gains an outside commit just before the swap."""

    moved: list[str] = field(default_factory=list)

    async def git(self, *args: str, env: Mapping[str, str] | None = None) -> str:
        if args[:1] == ("update-ref",) and not self.moved:
            outside = git(
                self.path, "commit-tree", f"{TARGET}^{{tree}}", "-p", TARGET, "-m", "x"
            )
            git(self.path, "update-ref", f"refs/heads/{TARGET}", outside)
            self.moved.append(outside)
        return await super().git(*args, env=env)


@dataclass(frozen=True)
class Raced:
    """Wraps a strategy, handing it a host that moves the target mid-landing.

    ``moved`` collects the outside commit, so a test can see it still stands.
    """

    inner: IntegrationStrategy
    moved: list[str] = field(default_factory=list)

    async def integrate(self, repo: GitRepo, series: PatchSeries) -> IntegrationReport:
        racing = MovesOnSwap(repo.path, repo.common_dir, moved=self.moved)
        return await self.inner.integrate(racing, series)


async def test_a_target_moved_before_the_swap_is_refused_and_stays_moved(
    host_repo: Path,
) -> None:
    git(host_repo, "branch", TARGET)
    raced = Raced(Integration(TARGET))

    result = await a_run(host_repo).integrate(raced)

    assert_refused(result, "target_moved", host_repo)
    (outside,) = raced.moved
    assert git(host_repo, "rev-parse", TARGET) == outside, "their commit stands"


async def test_a_swap_that_fails_with_the_target_unmoved_is_not_target_moved(
    host_repo: Path,
) -> None:
    git(host_repo, "branch", TARGET)
    tip = git(host_repo, "rev-parse", TARGET)
    # Another git process holds the ref's lock: the swap fails, nothing moved.
    (host_repo / ".git" / "refs" / "heads" / f"{TARGET}.lock").write_bytes(b"")

    result = await a_run(host_repo).integrate(TARGET)

    assert isinstance(result, RunFailed)
    assert result.stage == "integrate"
    assert isinstance(result.failure, CommandFailed)
    assert result.preserved == f"waystation/{result.run_id}"
    assert git(host_repo, "rev-parse", TARGET) == tip


async def test_a_tag_named_like_the_target_does_not_stand_in_for_it(
    host_repo: Path,
) -> None:
    git(host_repo, "branch", TARGET)
    commit_on(host_repo, "elsewhere", {"other.txt": "other\n"})
    git(host_repo, "tag", TARGET, "elsewhere")
    tip = git(host_repo, "rev-parse", f"refs/heads/{TARGET}")

    result = await a_run(host_repo).integrate(TARGET)

    assert isinstance(result, RunSucceeded)
    assert result.report is not None
    assert result.report.target_before == tip
    assert git(host_repo, "rev-parse", f"refs/heads/{TARGET}^") == tip
