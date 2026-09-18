"""Conflicts and named-target refusals, through the public run API (issue #24)."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path

import pytest

from helpers import a_run, git
from waystation import RunConflicted, RunSucceeded, ScriptedCommit, Summary
from waystation.integration import Conflict, FailedPatch

pytestmark = pytest.mark.git

TARGET = "agents/batch"


def commit_on(repo: Path, branch: str, files: Mapping[str, str]) -> str:
    """Commit ``files`` on ``branch`` (made at HEAD if missing); return its tip.

    The host's checkout comes back to where it was. Bytes are written as-is, so
    a line ending never differs between the host and the agent's series.
    """
    home = git(repo, "symbolic-ref", "--short", "HEAD")
    exists = git(repo, "branch", "--list", branch) != ""
    git(repo, "checkout", *(() if exists else ("-b",)), branch)
    for path, text in files.items():
        (repo / path).write_bytes(text.encode())
    git(repo, "add", *files)
    git(repo, "commit", "-m", f"outside: {', '.join(files)}")
    git(repo, "checkout", home)
    return git(repo, "rev-parse", branch)


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
    preserved = git(host_repo, "log", "--format=%s", f"{base}..{result.preserved}")
    assert preserved.splitlines() == ["claim shared", "add notes"]


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


async def test_a_conflict_leaves_the_host_as_it_was(host_repo: Path) -> None:
    commit_on(host_repo, TARGET, {"shared.txt": "target\n"})
    # A host mid-edit, so an index or tree that was touched can't look clean.
    (host_repo / "staged.txt").write_bytes(b"staged\n")
    git(host_repo, "add", "staged.txt")
    (host_repo / "README").write_bytes(b"edited, not staged\n")
    (host_repo / "scratch.log").write_bytes(b"untracked\n")
    before = host_state(host_repo)

    result = await a_run(host_repo, commits=CLAIMS_SHARED).integrate(TARGET)

    assert isinstance(result, RunConflicted)
    after = host_state(host_repo)
    preserved = f"refs/heads/{result.preserved} "
    after["refs"] = "\n".join(
        line
        for line in str(after["refs"]).splitlines()
        if not line.startswith(preserved)
    )
    assert after == before


async def test_a_conflict_ends_the_run_without_firing_integrated(
    host_repo: Path,
) -> None:
    commit_on(host_repo, TARGET, {"shared.txt": "target\n"})
    fired: list[str] = []

    result = await (
        a_run(host_repo, commits=CLAIMS_SHARED)
        .integrate(TARGET)
        .on_integrated(lambda ctx, report: fired.append("integrated"))
        .on_run_end(lambda ctx, result: fired.append(type(result).__name__))
    )

    assert isinstance(result, RunConflicted)
    assert fired == ["RunConflicted"]


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
    lifecycle = [r.getMessage() for r in caplog.records if r.name == "waystation.run"]
    assert "integrated" not in [message.split(":")[0] for message in lifecycle]
    end = lifecycle[-1]
    assert end.startswith("run end: conflicted")
    assert TARGET in end
    assert f"{result.preserved}" in end
