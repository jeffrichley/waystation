"""NoSandbox takes its process-tree handling as an injected strategy (ADR-0023)."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from waystation import Flow, NoSandbox, RunContext, RunFailed, ScriptedAgent
from waystation.agents import AgentLine
from waystation.sandbox import ProcessTree, host_process_trees


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


class _RecordedTree:
    def __init__(self, inner: ProcessTree, events: list[str]) -> None:
        self.inner = inner
        self.events = events

    def kill(self) -> None:
        self.events.append("kill")
        self.inner.kill()

    def release(self) -> None:
        self.events.append("release")
        self.inner.release()


class RecordingTrees:
    """Wraps the host's strategy and records what the sandbox asks of it."""

    def __init__(self) -> None:
        self.inner = host_process_trees()
        self.events: list[str] = []

    def spawn_options(self) -> dict[str, Any]:
        return self.inner.spawn_options()

    def adopt(self, process: asyncio.subprocess.Process) -> ProcessTree:
        self.events.append("adopt")
        return _RecordedTree(self.inner.adopt(process), self.events)


@pytest.mark.git
@pytest.mark.asyncio
async def test_injected_strategy_kills_a_stopped_agent_and_releases_every_exec(
    host_repo: Path,
) -> None:
    trees = RecordingTrees()
    flow = Flow(
        host_repo,
        agent=ScriptedAgent(lines=["first"], linger=True),
        sandbox=NoSandbox(process_trees=trees),
    )

    def stop(ctx: RunContext, line: AgentLine) -> None:
        raise RuntimeError("stop the agent")

    result = await flow.run("stop", outcome=Answer).on_agent_output(stop)

    assert isinstance(result, RunFailed)
    assert trees.events.count("kill") == 1
    assert trees.events.count("release") == trees.events.count("adopt")
    last_release = len(trees.events) - 1 - trees.events[::-1].index("release")
    assert trees.events.index("kill") < last_release
