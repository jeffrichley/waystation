"""Collect, salvage, and preservation (issue #22)."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from helpers import commit_on, git
from waystation import (
    AgentExited,
    Flow,
    NoSandbox,
    Refused,
    RunFailed,
    RunSucceeded,
    ScriptedAgent,
    ScriptedCommit,
    Series,
)
from waystation.agents.outcome import OUTCOME_MARKER, find_outcome
from waystation.agents.protocol import AgentCommand, AgentEvent, OutcomeReported
from waystation.agents.scripted import _find_sh, _shell_single_quote


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


class _MergeAgent:
    """Creates a merge commit so collect refuses nonlinear series."""

    def __init__(self, exit_code: int = 0) -> None:
        self.exit_code = exit_code

    def preflight(self) -> None:
        return None

    def command(self, prompt: str, outcome_schema: dict[str, Any]) -> AgentCommand:
        del prompt, outcome_schema
        payload = json.dumps({"summary": "merge"})
        marker = f"{OUTCOME_MARKER} {payload}"
        script = "\n".join(
            [
                "set -e",
                "base=$(git rev-parse HEAD)",
                "printf 'a\\n' > A",
                "git add A && git commit -m side-a",
                'git branch other "$base"',
                "git checkout other",
                "printf 'b\\n' > B",
                "git add B && git commit -m side-b",
                "git checkout -",
                "git merge --no-ff -m merge-commit other",
                f"printf '%s\\n' {_shell_single_quote(marker)}",
                f"exit {self.exit_code}",
            ]
        )
        return AgentCommand(
            argv=(_find_sh(), "-c", script),
            stdin=None,
            env={},
            pass_env=(),
        )

    def parse(self, line: str) -> list[AgentEvent]:
        raw = find_outcome(line)
        if raw is not None:
            return [OutcomeReported(raw=raw)]
        return []


@pytest.mark.git
@pytest.mark.asyncio
async def test_nonlinear_series_refused_and_squashed(host_repo: Path) -> None:
    flow = Flow(host_repo, agent=_MergeAgent(), sandbox=NoSandbox())
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
    flow = Flow(host_repo, agent=_MergeAgent(exit_code=4), sandbox=NoSandbox())

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
