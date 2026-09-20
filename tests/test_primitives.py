"""The stages compose by hand from public primitives, without a Flow."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel

from helpers import MAKES_A_MERGE, ShellAgent, git
from waystation import (
    Integration,
    NoSandbox,
    Refused,
    ScriptedAgent,
    ScriptedCommit,
    StageError,
    Summary,
    collect,
    integrate,
    prepare_workspace,
    run_agent,
)


class Answer(BaseModel):
    summary: str


@pytest.mark.git
@pytest.mark.asyncio
async def test_loop_composed_by_hand_lands_the_series(host_repo: Path) -> None:
    agent = ScriptedAgent(
        commits=(ScriptedCommit(message="by hand", files={"hand.txt": "made\n"}),),
        outcome=Answer(summary="done"),
    )

    workspace = await prepare_workspace(host_repo)
    async with NoSandbox().start(workspace, env={}) as sandbox:
        exit, outcome = await run_agent(
            sandbox, agent, agent.command("by hand", {}), Answer
        )
        series = await collect(sandbox, workspace)
    report = await integrate(host_repo, series, Integration("agents/by-hand"))

    assert exit.exit_code == 0
    assert outcome == Answer(summary="done")
    assert series.commits == 1
    assert not series.salvaged
    assert report.landed
    assert git(host_repo, "log", "-1", "--format=%s", "agents/by-hand") == "by hand"


@pytest.mark.git
@pytest.mark.asyncio
async def test_collect_refuses_a_nonlinear_series_rather_than_squashing_it_quietly(
    host_repo: Path,
) -> None:
    """A hand-composed loop hears the refusal, so it cannot land the squash."""
    agent = ShellAgent(MAKES_A_MERGE)

    workspace = await prepare_workspace(host_repo)
    async with NoSandbox().start(workspace, env={}) as sandbox:
        await run_agent(sandbox, agent, agent.command("merge", {}), Summary)
        with pytest.raises(StageError) as raised:
            await collect(sandbox, workspace)

    err = raised.value
    assert err.stage == "collect"
    assert isinstance(err.failure, Refused)
    assert err.failure.reason == "nonlinear_series"
    # The squash rides along, so a caller can still keep the work.
    assert err.series is not None
    assert err.series.commits == 1
