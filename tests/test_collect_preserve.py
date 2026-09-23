"""Collect, salvage, and preservation (issue #22)."""

from __future__ import annotations

import logging
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from helpers import (
    MAKES_A_MERGE,
    OK_OUTCOME,
    OK_OUTCOME_LINE,
    ShellAgent,
    commit_on,
    git,
)
from waystation import (
    AgentExited,
    CommandFailed,
    Flow,
    NoSandbox,
    Refused,
    RunFailed,
    RunSucceeded,
    Series,
)
from waystation.testing import ScriptedAgent, ScriptedCommit


class Answer(BaseModel):
    summary: str


def _host_head(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD")


def _host_porcelain(repo: Path) -> str:
    return git(repo, "status", "--porcelain")


@pytest.mark.git
@pytest.mark.asyncio
async def test_committed_work_preserved_on_host_branch(host_repo: Path) -> None:
    before_head = _host_head(host_repo)
    flow = Flow(
        host_repo,
        agent=ScriptedAgent(
            commits=(
                ScriptedCommit(
                    message="add note",
                    files={"NOTE": "hello\n"},
                ),
            ),
            outcome=Answer(summary="ok"),
        ),
        sandbox=NoSandbox(),
    )
    result = await flow.run("commit", outcome=Answer)

    assert isinstance(result, RunSucceeded)
    assert result.series is not None
    assert result.series.commits == 1
    assert result.series.salvaged is False
    assert result.preserved == f"waystation/{result.run_id}"
    assert _host_head(host_repo) == before_head
    assert _host_porcelain(host_repo) == ""
    tip = git(host_repo, "rev-parse", result.preserved)
    assert tip != before_head
    assert git(host_repo, "log", "-1", "--format=%s", tip) == "add note"
    assert "hello" in git(host_repo, "show", f"{tip}:NOTE")


@pytest.mark.git
@pytest.mark.asyncio
async def test_empty_series_creates_no_branch(host_repo: Path) -> None:
    before = set(
        git(host_repo, "for-each-ref", "--format=%(refname:short)").splitlines()
    )
    flow = Flow(
        host_repo,
        agent=ScriptedAgent(outcome=Answer(summary="noop")),
        sandbox=NoSandbox(),
    )
    result = await flow.run("noop", outcome=Answer)
    assert isinstance(result, RunSucceeded)
    assert result.series == Series(commits=0, salvaged=False)
    assert result.preserved is None
    after = set(
        git(host_repo, "for-each-ref", "--format=%(refname:short)").splitlines()
    )
    assert after == before


@pytest.mark.git
@pytest.mark.asyncio
async def test_uncommitted_work_is_salvaged_by_default(host_repo: Path) -> None:
    flow = Flow(
        host_repo,
        agent=ScriptedAgent(
            uncommitted={"DIRTY": "left behind\n"},
            outcome=Answer(summary="ok"),
        ),
        sandbox=NoSandbox(),
    )
    result = await flow.run("salvage", outcome=Answer)
    assert isinstance(result, RunSucceeded)
    assert result.series is not None
    assert result.series.commits == 1
    assert result.series.salvaged is True
    assert result.preserved is not None
    tip = git(host_repo, "rev-parse", result.preserved)
    msg = git(host_repo, "log", "-1", "--format=%B", tip)
    assert "WIP: salvaged uncommitted work" in msg
    assert f"Waystation-Run: {result.run_id}" in msg
    assert "left behind" in git(host_repo, "show", f"{tip}:DIRTY")


@pytest.mark.git
@pytest.mark.asyncio
async def test_salvage_false_drops_uncommitted_work(host_repo: Path) -> None:
    flow = Flow(
        host_repo,
        agent=ScriptedAgent(
            uncommitted={"DIRTY": "gone\n"},
            outcome=Answer(summary="ok"),
        ),
        sandbox=NoSandbox(),
        salvage=False,
    )
    result = await flow.run("drop", outcome=Answer)
    assert isinstance(result, RunSucceeded)
    assert result.series == Series(commits=0, salvaged=False)
    assert result.preserved is None


@pytest.mark.git
@pytest.mark.asyncio
async def test_agent_failure_still_preserves_series(host_repo: Path) -> None:
    flow = Flow(
        host_repo,
        agent=ScriptedAgent(
            commits=(ScriptedCommit(message="kept", files={"KEPT": "yes\n"}),),
            outcome=Answer(summary="also"),
            exit_code=3,
        ),
        sandbox=NoSandbox(),
    )
    result = await flow.run("fail", outcome=Answer)
    assert isinstance(result, RunFailed)
    assert isinstance(result.failure, AgentExited)
    assert result.series is not None
    assert result.series.commits == 1
    assert result.preserved == f"waystation/{result.run_id}"
    tip = git(host_repo, "rev-parse", result.preserved)
    assert "yes" in git(host_repo, "show", f"{tip}:KEPT")


@pytest.mark.git
@pytest.mark.asyncio
async def test_stale_index_lock_does_not_cost_the_series(host_repo: Path) -> None:
    # A git process killed mid-write leaves .git/index.lock behind.
    flow = Flow(
        host_repo,
        agent=ScriptedAgent(
            commits=(ScriptedCommit(message="kept", files={"KEPT": "yes\n"}),),
            uncommitted={"LEFT": "over\n", ".git/index.lock": ""},
            exit_code=3,
        ),
        sandbox=NoSandbox(),
    )

    result = await flow.run("killed mid-git", outcome=Answer)

    assert isinstance(result, RunFailed)
    assert isinstance(result.failure, AgentExited)
    assert result.series == Series(commits=2, salvaged=True)
    assert result.preserved == f"waystation/{result.run_id}"
    assert "over" in git(host_repo, "show", f"{result.preserved}:LEFT")


@pytest.mark.git
@pytest.mark.asyncio
async def test_collect_failing_after_agent_failure_is_logged_not_reported(
    host_repo: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # A stale HEAD.lock blocks the salvage commit whatever index it uses.
    flow = Flow(
        host_repo,
        agent=ScriptedAgent(
            uncommitted={"LEFT": "over\n", ".git/HEAD.lock": ""},
            exit_code=3,
        ),
        sandbox=NoSandbox(),
    )

    with caplog.at_level(logging.ERROR, logger="waystation"):
        result = await flow.run("unsalvageable", outcome=Answer)

    assert isinstance(result, RunFailed)
    assert result.stage == "agent"
    assert isinstance(result.failure, AgentExited)
    assert result.failure.exit_code == 3
    assert any("collect" in record.getMessage() for record in caplog.records)


def _git_execs(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Each git command line the sandbox ran: collect's, as the agent runs in sh."""
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == "waystation.sandbox" and r.getMessage().startswith("git ")
    ]


@pytest.mark.git
@pytest.mark.parametrize(
    ("left", "execs"),
    [({}, 1), ({"LEFT": "over\n"}, 4)],
    ids=["committed", "salvaged"],
)
async def test_collect_takes_one_exec_and_a_salvage_only_its_own_commit(
    host_repo: Path,
    caplog: pytest.LogCaptureFixture,
    left: dict[str, str],
    execs: int,
) -> None:
    # An exec is a `docker exec` on DockerSandbox, ~0.5 s on Docker Desktop:
    # the checks and the series are one, and a salvage adds its add and
    # commit and a second look (ADR-0029).
    flow = Flow(
        host_repo,
        agent=ScriptedAgent(
            commits=(ScriptedCommit(message="kept", files={"KEPT": "yes\n"}),),
            uncommitted=left,
            outcome=Answer(summary="ok"),
        ),
        sandbox=NoSandbox(),
    )

    with caplog.at_level(logging.DEBUG, logger="waystation.sandbox"):
        result = await flow.run("count", outcome=Answer)

    assert isinstance(result, RunSucceeded), result
    assert result.series == Series(commits=1 + len(left), salvaged=bool(left))
    assert len(_git_execs(caplog)) == execs


@pytest.mark.git
@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows looks a program up on the host's PATH, not the one a "
    "sandbox is given; there git finds its own sh wherever it is installed",
)
async def test_collect_needs_git_on_the_sandbox_path_and_no_sh(
    host_repo: Path, tmp_path: Path
) -> None:
    # Git runs collect's script with its own sh, so a host whose sh is not on
    # PATH — Windows, with only Git\cmd there — collects all the same.
    found = shutil.which("git")
    assert found is not None
    only_git = tmp_path / "only-git"
    only_git.mkdir()
    (only_git / "git").symlink_to(found)
    flow = Flow(
        host_repo,
        agent=ScriptedAgent(
            commits=(ScriptedCommit(message="kept", files={"KEPT": "yes\n"}),),
            uncommitted={"LEFT": "over\n"},
            outcome=Answer(summary="ok"),
        ),
        sandbox=NoSandbox(env={"PATH": str(only_git)}),
    )

    result = await flow.run("only git", outcome=Answer)

    assert isinstance(result, RunSucceeded), result
    assert result.series == Series(commits=2, salvaged=True)


@pytest.mark.git
@pytest.mark.parametrize(
    ("agent", "command"),
    [
        # A corrupt index fails the first look, before anything else runs.
        (
            ScriptedAgent(uncommitted={".git/index": "garbage"}, outcome=OK_OUTCOME),
            ("status", "--porcelain"),
        ),
        # format-patch alone reads format.*, so every check before it passes.
        (
            ShellAgent(
                "git config format.numbered bogus && printf x > a.txt && "
                f"git add a.txt && git commit -qm one && echo '{OK_OUTCOME_LINE}'"
            ),
            ("format-patch", "--stdout", "{base}..HEAD"),
        ),
    ],
    ids=["status", "format-patch"],
)
async def test_a_git_failure_in_collect_names_its_command_and_only_its_stderr(
    host_repo: Path, agent: Any, command: tuple[str, ...]
) -> None:
    result = await Flow(host_repo, agent=agent, sandbox=NoSandbox()).run("break git")

    assert isinstance(result, RunFailed), result
    assert result.stage == "collect"
    failure = result.failure
    assert isinstance(failure, CommandFailed)
    assert result.base_sha is not None
    expected = ("git", *(arg.format(base=result.base_sha) for arg in command))
    assert failure.argv == expected
    assert failure.exit_code == 128
    # Git's own words and nothing before them: no other step's stderr.
    assert failure.stderr_tail.startswith(("fatal: ", "error: ")), failure


@pytest.mark.git
@pytest.mark.asyncio
async def test_nonlinear_series_refused_and_squashed(host_repo: Path) -> None:
    flow = Flow(host_repo, agent=ShellAgent(MAKES_A_MERGE), sandbox=NoSandbox())
    result = await flow.run("merge", outcome=Answer)
    assert isinstance(result, RunFailed)
    assert result.stage == "collect"
    assert isinstance(result.failure, Refused)
    assert result.failure.reason == "nonlinear_series"
    assert result.preserved is not None
    assert result.series is not None
    assert result.series.commits == 1
    tip = git(host_repo, "rev-parse", result.preserved)
    assert git(host_repo, "rev-list", "--count", f"{result.base_sha}..{tip}") == "1"


@pytest.mark.git
@pytest.mark.asyncio
async def test_agent_failure_outranks_a_nonlinear_series(host_repo: Path) -> None:
    agent = ShellAgent(MAKES_A_MERGE + "\nexit 4")
    flow = Flow(host_repo, agent=agent, sandbox=NoSandbox())

    result = await flow.run("merge then fail", outcome=Answer)

    assert isinstance(result, RunFailed)
    assert result.stage == "agent"
    assert isinstance(result.failure, AgentExited)
    assert result.failure.exit_code == 4
    assert result.preserved == f"waystation/{result.run_id}"


@pytest.mark.git
async def test_patch_series_from_range(host_repo: Path) -> None:
    from waystation import PatchSeries

    base = git(host_repo, "rev-parse", "HEAD")
    commit_on(host_repo, "feature", {"X": "x\n"}, message="on feature")
    series = await PatchSeries.from_range(host_repo, base, "feature")
    assert series.commits == 1
    assert series.base_sha == base
    assert "on feature" in series.patches[0]


@pytest.mark.git
async def test_a_range_with_a_merge_in_it_is_refused_not_flattened(
    host_repo: Path,
) -> None:
    # A forge's "Update branch" merges the target into a ticket branch; a
    # series built past it must say so, never drop the merge quietly (#138).
    from waystation import PatchSeries, StageError

    base = git(host_repo, "rev-parse", "HEAD")
    commit_on(host_repo, "side", {"S": "s\n"}, message="on side")
    commit_on(host_repo, "feature", {"X": "x\n"}, message="on feature")
    git(host_repo, "checkout", "-q", "feature")
    git(host_repo, "merge", "-q", "--no-ff", "-m", "update branch", "side")
    git(host_repo, "checkout", "-q", "-")

    with pytest.raises(StageError) as raised:
        await PatchSeries.from_range(host_repo, base, "feature")

    assert raised.value.stage is None
    assert isinstance(raised.value.failure, Refused)
    assert raised.value.failure.reason == "nonlinear_series"


@pytest.mark.git
async def test_a_range_whose_base_is_not_an_ancestor_is_refused(
    host_repo: Path,
) -> None:
    # The patches would be cut against a commit the series does not start at.
    from waystation import PatchSeries, StageError

    moved = commit_on(host_repo, "target", {"T": "t\n"}, message="target moved")
    commit_on(host_repo, "feature", {"X": "x\n"}, message="on feature")

    with pytest.raises(StageError) as raised:
        await PatchSeries.from_range(host_repo, moved, "feature")

    assert isinstance(raised.value.failure, Refused)
    assert raised.value.failure.reason == "nonlinear_series"
    # The series itself is linear: say what is wrong, and what to pass.
    assert "does not descend from" in raised.value.failure.detail
    assert "merge-base" in raised.value.failure.detail


_REWINDS_BELOW_BASE = "\n".join(
    ["set -e", "git reset -q --hard HEAD~1", f"echo '{OK_OUTCOME_LINE}'"]
)
_COMMITS_AN_ORPHAN = "\n".join(
    [
        "set -e",
        "git checkout -q --orphan lone",
        "printf 'lone\\n' > LONE",
        "git add LONE && git commit -q -m lone",
        f"echo '{OK_OUTCOME_LINE}'",
    ]
)


@pytest.mark.git
@pytest.mark.parametrize(
    ("script", "tree"),
    [(_REWINDS_BELOW_BASE, ["README"]), (_COMMITS_AN_ORPHAN, ["B", "LONE", "README"])],
    ids=["rewind", "orphan"],
)
async def test_a_head_that_does_not_descend_from_base_is_refused_and_kept_as_one_commit(
    host_repo: Path, script: str, tree: list[str]
) -> None:
    """Not only a merge is nonlinear: so is any HEAD base is not an ancestor of.

    What the agent left is kept all the same, as one commit on base whose
    tree is the agent's (ADR-0006).
    """
    # A second commit, so the base has a parent to rewind to.
    commit_on(host_repo, git(host_repo, "branch", "--show-current"), {"B": "b\n"})

    result = await Flow(host_repo, agent=ShellAgent(script), sandbox=NoSandbox()).run(
        "go below base"
    )

    assert isinstance(result, RunFailed), result
    assert result.stage == "collect"
    assert isinstance(result.failure, Refused)
    assert result.failure.reason == "nonlinear_series"
    assert result.preserved == f"waystation/{result.run_id}"
    assert result.series is not None
    assert result.series.commits == 1
    assert git(host_repo, "rev-parse", f"{result.preserved}~1") == result.base_sha
    assert git(host_repo, "ls-tree", "--name-only", result.preserved).split() == tree
