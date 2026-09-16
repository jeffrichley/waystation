"""Collect, salvage, and preservation (issue #22)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

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


@pytest.fixture
def host_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "host"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Waystation Test")
    _git(repo, "config", "user.email", "test@waystation.example")
    (repo / "README").write_text("committed\n", encoding="utf-8")
    _git(repo, "add", "README")
    _git(repo, "commit", "-m", "init")
    return repo


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _host_head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD")


def _host_porcelain(repo: Path) -> str:
    return _git(repo, "status", "--porcelain")


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
    tip = _git(host_repo, "rev-parse", result.preserved)
    assert tip != before_head
    assert _git(host_repo, "log", "-1", "--format=%s", tip) == "add note"
    assert "hello" in _git(host_repo, "show", f"{tip}:NOTE")


@pytest.mark.git
@pytest.mark.asyncio
async def test_empty_series_creates_no_branch(host_repo: Path) -> None:
    before = set(
        _git(host_repo, "for-each-ref", "--format=%(refname:short)").splitlines()
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
        _git(host_repo, "for-each-ref", "--format=%(refname:short)").splitlines()
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
    tip = _git(host_repo, "rev-parse", result.preserved)
    msg = _git(host_repo, "log", "-1", "--format=%B", tip)
    assert "WIP: salvaged uncommitted work" in msg
    assert f"Waystation-Run: {result.run_id}" in msg
    assert "left behind" in _git(host_repo, "show", f"{tip}:DIRTY")


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
    tip = _git(host_repo, "rev-parse", result.preserved)
    assert "yes" in _git(host_repo, "show", f"{tip}:KEPT")


class _MergeAgent:
    """Creates a merge commit so collect refuses nonlinear series."""

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
                "exit 0",
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
    tip = _git(host_repo, "rev-parse", result.preserved)
    assert _git(host_repo, "rev-list", "--count", f"{result.base_sha}..{tip}") == "1"


@pytest.mark.git
def test_patch_series_from_range(host_repo: Path) -> None:
    from waystation import PatchSeries

    _git(host_repo, "checkout", "-b", "feature")
    (host_repo / "X").write_text("x\n", encoding="utf-8")
    _git(host_repo, "add", "X")
    _git(host_repo, "commit", "-m", "on feature")
    base = _git(host_repo, "rev-parse", "feature^")
    series = PatchSeries.from_range(host_repo, base, "feature")
    assert series.commits == 1
    assert series.base_sha == base
    assert "on feature" in series.patches[0]
