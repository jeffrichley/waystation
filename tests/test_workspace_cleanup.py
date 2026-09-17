"""A run leaves no workspace behind, even with git's read-only objects."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from pydantic import BaseModel

from waystation import (
    Flow,
    NoSandbox,
    RunContext,
    RunFailed,
    RunSucceeded,
    ScriptedAgent,
    ScriptedCommit,
)


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


def _workspaces(temp: Path) -> list[Path]:
    return sorted(temp.glob("waystation-*"))


@pytest.mark.git
@pytest.mark.asyncio
async def test_finished_run_removes_its_workspace(
    host_repo: Path, isolated_tempdir: Path
) -> None:
    flow = Flow(
        host_repo,
        agent=ScriptedAgent(
            commits=(ScriptedCommit(message="work", files={"w.txt": "x\n"}),),
            outcome=Answer(summary="ok"),
        ),
        sandbox=NoSandbox(),
    )

    result = await flow.run("work", outcome=Answer)

    assert isinstance(result, RunSucceeded)
    assert _workspaces(isolated_tempdir) == []


@pytest.mark.git
@pytest.mark.asyncio
async def test_run_failing_before_its_sandbox_removes_its_workspace(
    host_repo: Path, isolated_tempdir: Path
) -> None:
    flow = Flow(
        host_repo,
        agent=ScriptedAgent(outcome=Answer(summary="ok")),
        sandbox=NoSandbox(),
    )

    def broken(ctx: RunContext) -> None:
        raise RuntimeError("workspace_ready bug")

    result = await flow.run("fail", outcome=Answer).on_workspace_ready(broken)

    assert isinstance(result, RunFailed)
    assert result.stage == "workspace"
    assert _workspaces(isolated_tempdir) == []
