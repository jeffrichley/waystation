"""Squash: a run's series lands on its target as one commit (#81, ADR-0040).

``Squash`` is built only from the landing steps ``GitRepo`` offers any
strategy author, so what holds here holds for a user's strategy too.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from helpers import a_run, awaited, commit_on, git, host_state, subjects
from waystation import (
    Refused,
    RunConflicted,
    RunFailed,
    RunSucceeded,
    ScriptedCommit,
    Squash,
)
from waystation.integration import Conflict

pytestmark = pytest.mark.git

TARGET = "agents/squashed"

THREE = (
    ScriptedCommit("add notes", {"notes.txt": "n\n"}),
    ScriptedCommit("add code", {"code.txt": "c\n"}),
    ScriptedCommit("fix notes", {"notes.txt": "fixed\n"}),
)


async def test_a_squashed_series_lands_as_one_commit_holding_its_net_change(
    host_repo: Path,
) -> None:
    base = git(host_repo, "rev-parse", "HEAD")
    before = host_state(host_repo)

    result = await a_run(host_repo, commits=THREE).integrate(Squash(TARGET))

    assert isinstance(result, RunSucceeded)
    report = result.report
    assert report is not None
    assert (report.strategy, report.target, report.mechanism) == (
        "Squash",
        TARGET,
        "squash",
    )
    (landed,) = report.landed
    assert report.target_before == base
    assert report.target_after == landed == git(host_repo, "rev-parse", TARGET)
    assert git(host_repo, "rev-list", "--count", f"{base}..{TARGET}") == "1"
    assert git(host_repo, "rev-parse", f"{TARGET}^") == base
    assert git(host_repo, "show", f"{TARGET}:notes.txt") == "fixed"
    assert git(host_repo, "show", f"{TARGET}:code.txt") == "c"
    assert result.preserved is None
    after = host_state(host_repo, ignoring=TARGET)
    assert after == before, "only the target moved: no checkout, index or file"


async def test_the_squash_keeps_the_agents_authorship_and_lists_the_subjects(
    host_repo: Path,
) -> None:
    result = await a_run(host_repo, commits=THREE).integrate(Squash(TARGET))

    assert isinstance(result, RunSucceeded)
    message = git(host_repo, "log", "-1", "--format=%B", TARGET)
    assert message == "add notes\n\n* add notes\n* add code\n* fix notes"
    author = git(host_repo, "log", "-1", "--format=%an <%ae>", TARGET)
    committer = git(host_repo, "log", "-1", "--format=%cn <%ce>", TARGET)
    host = git(host_repo, "config", "user.name")
    assert author == committer == f"{host} <{git(host_repo, 'config', 'user.email')}>"


async def test_a_one_commit_series_keeps_its_own_message(host_repo: Path) -> None:
    result = await a_run(host_repo).integrate(Squash(TARGET))

    assert isinstance(result, RunSucceeded)
    assert git(host_repo, "log", "-1", "--format=%B", TARGET) == "add a file"


async def test_a_flow_script_can_name_the_squash_commit(host_repo: Path) -> None:
    squash = Squash(TARGET, message="feat: notes\n\nWritten by an agent.")

    result = await a_run(host_repo, commits=THREE).integrate(squash)

    assert isinstance(result, RunSucceeded)
    assert git(host_repo, "log", "-1", "--format=%B", TARGET) == (
        "feat: notes\n\nWritten by an agent."
    )


# Claims shared.txt, then gives it back: patch by patch the first commit
# conflicts, but as a net change the series never touches it.
GIVES_BACK = (
    ScriptedCommit("claim shared", {"shared.txt": "agent\n"}),
    ScriptedCommit("give it back", {"shared.txt": "base\n"}),
    ScriptedCommit("add notes", {"notes.txt": "n\n"}),
)


async def test_a_squash_lands_where_apply_conflicts_partway(host_repo: Path) -> None:
    home = git(host_repo, "symbolic-ref", "--short", "HEAD")
    commit_on(host_repo, home, {"shared.txt": "base\n"})
    tip = commit_on(host_repo, TARGET, {"shared.txt": "target\n"})

    applied = await a_run(host_repo, commits=GIVES_BACK).integrate(TARGET)
    assert isinstance(applied, RunConflicted), "patch by patch, it conflicts"

    squashed = await a_run(host_repo, commits=GIVES_BACK).integrate(Squash(TARGET))

    assert isinstance(squashed, RunSucceeded), "only the net change is replayed"
    assert git(host_repo, "rev-parse", f"{TARGET}^") == tip
    assert git(host_repo, "show", f"{TARGET}:shared.txt") == "target"
    assert git(host_repo, "show", f"{TARGET}:notes.txt") == "n"


async def test_a_squash_that_conflicts_names_the_paths_and_keeps_the_series(
    host_repo: Path,
) -> None:
    base = git(host_repo, "rev-parse", "HEAD")
    tip = commit_on(host_repo, TARGET, {"notes.txt": "theirs\n"})
    before = host_state(host_repo)

    result = await a_run(host_repo, commits=THREE).integrate(Squash(TARGET))

    assert isinstance(result, RunConflicted)
    report = result.report
    assert report.conflict == Conflict(paths=("notes.txt",), failed_patch=None)
    assert (report.target_before, report.target_after, report.landed) == (
        tip,
        None,
        (),
    )
    assert result.preserved == f"waystation/{result.run_id}"
    assert subjects(host_repo, f"{base}..{result.preserved}") == [
        "fix notes",
        "add code",
        "add notes",
    ], "the series is kept unsquashed"
    assert host_state(host_repo, ignoring=result.preserved) == before


async def test_a_squash_onto_a_checked_out_target_is_refused(host_repo: Path) -> None:
    home = git(host_repo, "symbolic-ref", "--short", "HEAD")

    result = await a_run(host_repo).integrate(Squash(home))

    assert isinstance(result, RunFailed)
    assert isinstance(result.failure, Refused)
    assert result.failure.reason == "target_checked_out"
    assert result.preserved == f"waystation/{result.run_id}"


async def test_an_empty_series_squashes_to_nothing(host_repo: Path) -> None:
    result = await a_run(host_repo, commits=()).integrate(Squash(TARGET))

    assert isinstance(result, RunSucceeded)
    assert result.report is not None
    assert result.report.landed == ()
    assert result.report.target_after is None
    assert git(host_repo, "branch", "--list", TARGET) == ""


async def test_a_series_whose_net_change_is_already_there_lands_nothing(
    host_repo: Path,
) -> None:
    tip = commit_on(host_repo, TARGET, {"notes.txt": "n\n"})

    result = await a_run(
        host_repo, commits=(ScriptedCommit("add notes", {"notes.txt": "n\n"}),)
    ).integrate(Squash(TARGET))

    assert isinstance(result, RunSucceeded)
    assert result.report is not None
    assert result.report.landed == ()
    assert result.report.target_after == tip
    assert git(host_repo, "rev-parse", TARGET) == tip, "no empty commit"


async def test_concurrent_squashes_onto_one_target_each_land_one_commit(
    host_repo: Path,
) -> None:
    base = git(host_repo, "rev-parse", "HEAD")
    runs = [
        a_run(
            host_repo, commits=(ScriptedCommit(f"add {n}", {f"{n}.txt": n}),)
        ).integrate(Squash(TARGET))
        for n in ("one", "two", "three")
    ]

    results = await asyncio.gather(*(awaited(run) for run in runs))

    assert all(isinstance(result, RunSucceeded) for result in results)
    assert git(host_repo, "rev-list", "--count", f"{base}..{TARGET}") == "3"
