"""The stages compose by hand from public primitives, without a Flow."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from pydantic import BaseModel

from waystation import (
    Integration,
    NoSandbox,
    ScriptedAgent,
    ScriptedCommit,
    collect,
    integrate,
    prepare_workspace,
    run_agent,
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
    (repo / "README").write_bytes(b"committed\n")
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


@pytest.mark.git
@pytest.mark.asyncio
async def test_loop_composed_by_hand_lands_the_series(host_repo: Path) -> None:
    agent = ScriptedAgent(
        commits=(ScriptedCommit(message="by hand", files={"hand.txt": "made\n"}),),
        outcome=Answer(summary="done"),
    )

    workspace = prepare_workspace(host_repo)
    async with NoSandbox().start(workspace, env={}, pass_env=()) as sandbox:
        exit, outcome = await run_agent(
            sandbox, agent, agent.command("by hand", {}), Answer
        )
        collected = await collect(sandbox, workspace)
    report = await integrate(
        host_repo, collected.patch_series, Integration("agents/by-hand")
    )

    assert exit.exit_code == 0
    assert outcome == Answer(summary="done")
    assert collected.patch_series.commits == 1
    assert report.landed
    assert _git(host_repo, "log", "-1", "--format=%s", "agents/by-hand") == "by hand"
