"""Teardown comes before integration and never changes the result (ADR-0016)."""

from __future__ import annotations

import logging
import subprocess
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from pydantic import BaseModel

from waystation import (
    Flow,
    Integration,
    IntegrationReport,
    NoSandbox,
    RunContext,
    RunSucceeded,
    Sandbox,
    ScriptedAgent,
    ScriptedCommit,
    Workspace,
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


class TrackedSandbox:
    """NoSandbox that records its teardown, and can fail during it."""

    def __init__(self, events: list[str], *, teardown_error: bool = False) -> None:
        self.events = events
        self.teardown_error = teardown_error
        self.inner = NoSandbox()

    async def preflight(self) -> None:
        await self.inner.preflight()

    @asynccontextmanager
    async def start(
        self, ws: Workspace, *, env: Mapping[str, str], pass_env: Sequence[str]
    ) -> AsyncIterator[Sandbox]:
        async with self.inner.start(ws, env=env, pass_env=pass_env) as sandbox:
            yield sandbox
        self.events.append("teardown")
        if self.teardown_error:
            raise RuntimeError("teardown exploded")


def _committing_agent() -> ScriptedAgent:
    return ScriptedAgent(
        commits=(ScriptedCommit(message="feat", files={"f.txt": "x\n"}),),
        outcome=Answer(summary="ok"),
    )


@pytest.mark.git
@pytest.mark.asyncio
async def test_sandbox_is_torn_down_before_integration(host_repo: Path) -> None:
    events: list[str] = []
    flow = Flow(
        host_repo,
        agent=_committing_agent(),
        sandbox=TrackedSandbox(events),
        integration=Integration("agents/after-teardown"),
    )

    @flow.on_integrated
    def landed(ctx: RunContext, report: IntegrationReport) -> None:
        events.append("integrated")

    result = await flow.run("land", outcome=Answer)

    assert isinstance(result, RunSucceeded)
    assert events == ["teardown", "integrated"]


@pytest.mark.git
@pytest.mark.asyncio
async def test_teardown_error_is_logged_and_the_series_still_lands(
    host_repo: Path, caplog: pytest.LogCaptureFixture
) -> None:
    events: list[str] = []
    flow = Flow(
        host_repo,
        agent=_committing_agent(),
        sandbox=TrackedSandbox(events, teardown_error=True),
        integration=Integration("agents/despite-teardown"),
    )

    with caplog.at_level(logging.ERROR, logger="waystation"):
        result = await flow.run("land", outcome=Answer)

    assert isinstance(result, RunSucceeded)
    assert result.report is not None
    assert _git(host_repo, "log", "-1", "--format=%s", "agents/despite-teardown") == (
        "feat"
    )
    assert any("teardown exploded" in r.getMessage() for r in caplog.records)
