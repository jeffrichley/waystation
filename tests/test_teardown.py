"""Teardown comes before integration and never changes the result (ADR-0016)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from pydantic import BaseModel

from helpers import awaited, git, until, workspaces
from waystation import (
    CommandFailed,
    Flow,
    Integration,
    IntegrationReport,
    NoSandbox,
    RunContext,
    RunFailed,
    RunSucceeded,
    Sandbox,
    ScriptedAgent,
    ScriptedCommit,
    StageError,
    TimedOut,
    Timeouts,
    Workspace,
)
from waystation.clock import ManualClock, use_clock
from waystation.workspace import remove_workspace


class Answer(BaseModel):
    summary: str


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
    assert git(host_repo, "log", "-1", "--format=%s", "agents/despite-teardown") == (
        "feat"
    )
    assert any("teardown exploded" in r.getMessage() for r in caplog.records)


class FailsToStart:
    """A backend whose start fails, cleaning up after itself first.

    A context manager whose enter fails owns its own cleanup — ``async with``
    never calls ``__aexit__`` for it — so this one removes the workspace it
    was handed. ``hang`` makes it wait instead, until its bound gives up.
    """

    def __init__(self, *, hang: bool = False) -> None:
        self.hang = hang

    async def preflight(self) -> None:
        return None

    @asynccontextmanager
    async def start(
        self, ws: Workspace, *, env: Mapping[str, str], pass_env: Sequence[str]
    ) -> AsyncIterator[Sandbox]:
        try:
            if self.hang:
                await asyncio.Event().wait()
            raise StageError("sandbox", _NO_IMAGE)
        finally:
            remove_workspace(ws.path)
        yield  # unreachable: a start that fails never yields a sandbox


_NO_IMAGE = CommandFailed(
    argv=("docker", "run"), exit_code=125, stderr_tail="No such image"
)


@pytest.mark.git
async def test_a_sandbox_that_fails_to_start_fails_the_run_with_its_reason(
    host_repo: Path, isolated_tempdir: Path
) -> None:
    flow = Flow(host_repo, agent=_committing_agent(), sandbox=FailsToStart())

    result = await flow.run("start", outcome=Answer)

    assert isinstance(result, RunFailed)
    assert result.stage == "sandbox"
    assert result.failure == _NO_IMAGE
    assert workspaces(isolated_tempdir) == []


@pytest.mark.git
async def test_a_sandbox_start_past_its_bound_times_out(
    host_repo: Path, isolated_tempdir: Path
) -> None:
    clock = ManualClock()
    flow = Flow(
        host_repo,
        agent=_committing_agent(),
        sandbox=FailsToStart(hang=True),
        timeouts=Timeouts(sandbox=1.0),
    )

    with use_clock(clock):
        task = asyncio.create_task(awaited(flow.run("start", outcome=Answer)))
        await until(lambda: bool(clock._waiters), task)
        clock.advance(1.0)
        result = await task

    assert isinstance(result, RunFailed)
    assert result.stage == "sandbox"
    assert isinstance(result.failure, TimedOut)
    assert result.failure.bound == "sandbox"
    assert workspaces(isolated_tempdir) == []
