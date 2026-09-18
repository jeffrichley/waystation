"""A run leaves no workspace behind, even with git's read-only objects."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel

from helpers import workspaces
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
    assert workspaces(isolated_tempdir) == []


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
    assert workspaces(isolated_tempdir) == []
