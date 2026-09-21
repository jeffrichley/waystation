"""Resolving a conflict: a resolver run, or a preservation branch fixed by hand (#32).

Resolution is a run, not a seam (ADR-0015): the resolver starts at the target,
sees the preservation branch through ``.extra_refs()``, and replays it by
cherry-pick; a hand-resolved branch lands through the same ``integrate``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from helpers import OUTCOME, ShellAgent, a_run, commit_on, git, subjects, workspaces
from waystation import (
    Flow,
    Integration,
    NoSandbox,
    PatchSeries,
    Refused,
    RunConflicted,
    RunFailed,
    RunSpec,
    RunSucceeded,
    ScriptedCommit,
    Summary,
    integrate,
)

pytestmark = pytest.mark.git

TARGET = "agents/batch"


def _shell_run(repo: Path, script: str) -> RunSpec[Summary]:
    return Flow(repo, agent=ShellAgent(script), sandbox=NoSandbox()).run("resolve")


def _reports(expression: str) -> str:
    """A script line reporting ``expression``'s output as the Outcome's summary."""
    return f'printf \'%s{{"summary": "%s"}}\\n\' \'{OUTCOME}\' "$({expression})"'


async def test_an_extra_ref_is_visible_in_the_workspace_under_its_own_name(
    host_repo: Path,
) -> None:
    tip = commit_on(host_repo, "side/work", {"side.txt": "side\n"})

    result = await _shell_run(
        host_repo, _reports("git rev-parse refs/heads/side/work")
    ).extra_refs("side/work")

    assert isinstance(result, RunSucceeded), result
    assert result.outcome == Summary(summary=tip)


async def test_an_extra_ref_may_be_named_in_full(host_repo: Path) -> None:
    tip = commit_on(host_repo, "side/work", {"side.txt": "side\n"})

    result = await _shell_run(
        host_repo, _reports("git rev-parse refs/heads/side/work")
    ).extra_refs("refs/heads/side/work")

    assert isinstance(result, RunSucceeded), result
    assert result.outcome == Summary(summary=tip)


async def test_a_workspace_holds_the_extra_refs_and_nothing_else_of_the_hosts(
    host_repo: Path,
) -> None:
    """Naming a ref makes that ref travel, not the host's others (ADR-0037)."""
    commit_on(host_repo, "side/work", {"side.txt": "side\n"})
    commit_on(host_repo, "unrelated", {"u.txt": "u\n"})
    git(host_repo, "tag", "v1")

    result = await _shell_run(
        host_repo, _reports("git for-each-ref --format='%(refname)' | tr '\\n' ' '")
    ).extra_refs("side/work", "v1")

    assert isinstance(result, RunSucceeded), result
    assert sorted(result.outcome.summary.split()) == [
        "refs/heads/side/work",
        f"refs/heads/waystation/{result.run_id}",
        "refs/tags/v1",
    ]


@pytest.mark.parametrize(
    "ref", ["no/such/branch", "HEAD~1", "0" * 40, "HEAD", "origin/work"]
)
async def test_an_extra_ref_that_names_no_host_branch_or_tag_is_refused(
    host_repo: Path, isolated_tempdir: Path, ref: str
) -> None:
    """Only a branch or tag named as itself has a same name to travel under.

    Missing, a revision, ``HEAD`` (which would arrive as the branch it points
    at), or a remote's ref — and no remote travels (ADR-0037).
    """
    git(host_repo, "update-ref", "refs/remotes/origin/work", "HEAD")

    result = await a_run(host_repo).extra_refs(ref)

    assert isinstance(result, RunFailed), result
    assert result.stage == "workspace"
    assert isinstance(result.failure, Refused)
    assert result.failure.reason == "missing_extra_ref"
    assert ref in result.failure.detail
    assert workspaces(isolated_tempdir) == []


def _resolver(base: str, preserved: str) -> str:
    """Replay ``base..preserved`` onto the target by cherry-pick, taking both sides.

    What a resolver agent is asked to do (ADR-0015), scripted: the conflict
    is real, and the resolution is to keep the target's line and add ours.
    """
    return "\n".join(
        [
            "set -e",
            f"if ! git cherry-pick {base}..{preserved}; then",
            "  printf 'a\\nb\\n' > shared.txt",
            "  git add shared.txt",
            "  GIT_EDITOR=true git cherry-pick --continue",
            "fi",
            f"printf '%s\\n' '{OUTCOME}{{\"summary\": \"resolved\"}}'",
        ]
    )


async def test_a_resolver_run_lands_what_a_conflicted_run_preserved(
    host_repo: Path,
) -> None:
    base = git(host_repo, "rev-parse", "HEAD")
    git(host_repo, "branch", TARGET)
    first = await a_run(
        host_repo, commits=(ScriptedCommit("say a", {"shared.txt": "a\n"}),)
    ).integrate(TARGET)
    second = await a_run(
        host_repo, commits=(ScriptedCommit("say b", {"shared.txt": "b\n"}),)
    ).integrate(TARGET)
    assert isinstance(first, RunSucceeded), first
    assert isinstance(second, RunConflicted), second
    assert second.preserved is not None
    assert second.base_sha is not None

    resolved = await (
        _shell_run(host_repo, _resolver(second.base_sha, second.preserved))
        .base(TARGET)
        .extra_refs(second.preserved)
        .integrate(TARGET)
    )

    assert isinstance(resolved, RunSucceeded), resolved
    assert resolved.base_sha == git(host_repo, "rev-parse", f"{TARGET}~1")
    assert subjects(host_repo, f"{base}..{TARGET}") == ["say b", "say a"]
    assert git(host_repo, "show", f"{TARGET}:shared.txt") == "a\nb"


async def test_a_preservation_branch_resolved_by_hand_lands_through_integrate(
    host_repo: Path,
) -> None:
    base = git(host_repo, "rev-parse", "HEAD")
    commit_on(host_repo, TARGET, {"shared.txt": "a\n"})
    conflicted = await a_run(
        host_repo, commits=(ScriptedCommit("say b", {"shared.txt": "b\n"}),)
    ).integrate(TARGET)
    assert isinstance(conflicted, RunConflicted), conflicted
    preserved = conflicted.preserved
    assert preserved is not None

    # By hand: rebase the preserved series onto the target, fix, carry on.
    home = git(host_repo, "symbolic-ref", "--short", "HEAD")
    git(host_repo, "checkout", "-q", preserved)
    with pytest.raises(subprocess.CalledProcessError):
        git(host_repo, "rebase", TARGET)
    (host_repo / "shared.txt").write_bytes(b"a\nb\n")
    git(host_repo, "add", "shared.txt")
    git(host_repo, "-c", "core.editor=true", "rebase", "--continue")
    git(host_repo, "checkout", "-q", home)

    series = await PatchSeries.from_range(host_repo, TARGET, preserved)
    report = await integrate(host_repo, series, Integration(TARGET))

    assert len(report.landed) == 1
    assert subjects(host_repo, f"{base}..{TARGET}") == ["say b", "outside: shared.txt"]
    assert git(host_repo, "show", f"{TARGET}:shared.txt") == "a\nb"
