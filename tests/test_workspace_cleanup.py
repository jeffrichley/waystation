"""A run leaves no workspace behind, even with git's read-only objects."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
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
    Sandbox,
    ScriptedAgent,
    ScriptedCommit,
    Workspace,
    prepare_workspace,
)
from waystation import workspace as _workspace


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


@pytest.mark.git
async def test_a_composer_can_remove_a_workspace_it_made(host_repo: Path) -> None:
    """Through the public surface, on a host whose git objects are read-only.

    A composer who prepares a workspace and never starts a sandbox used to
    have no removal to reach for: a bare ``rmtree`` fails on Windows (#76).
    """
    workspace = await prepare_workspace(host_repo)
    assert workspace.path.exists()

    await workspace.remove()

    assert not workspace.path.exists()


@pytest.mark.git
async def test_a_backend_whose_teardown_fails_still_leaves_no_workspace(
    host_repo: Path, isolated_tempdir: Path
) -> None:
    """Removal is core's, so a backend cannot leak the dir by forgetting it."""
    flow = Flow(
        host_repo,
        agent=ScriptedAgent(
            commits=(ScriptedCommit(message="work", files={"w.txt": "x\n"}),),
            outcome=Answer(summary="ok"),
        ),
        sandbox=_BrokenTeardown(),
    )

    result = await flow.run("work", outcome=Answer)

    assert isinstance(result, RunSucceeded)
    assert workspaces(isolated_tempdir) == []


@pytest.mark.git
async def test_the_loop_keeps_turning_while_a_workspace_is_removed(
    host_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The removal waits on handles, so it waits on a thread, not on the loop.

    It patches the module's own removal because that is the seam under test:
    made to block, a removal that ran on the loop could never reach the
    ``release.set()`` below, so this test deadlocks rather than fails if the
    guarantee goes.
    """
    workspace = await prepare_workspace(host_repo)
    started = threading.Event()
    release = threading.Event()
    real = _workspace._remove_while_handles_close

    def blocking(path: Path) -> None:
        started.set()
        # Bounded so that a regression fails rather than hangs: if this ran on
        # the loop, nothing could ever reach the `release.set()` below, and an
        # unbounded wait would take the whole session down with pytest-timeout
        # rather than this one test.
        release.wait(timeout=30)
        real(path)

    monkeypatch.setattr(_workspace, "_remove_while_handles_close", blocking)

    removing = asyncio.create_task(workspace.remove())
    turns = 0
    while not started.is_set():
        await asyncio.sleep(0)
        turns += 1
    release.set()
    await asyncio.wait_for(removing, timeout=10)

    assert turns > 0
    assert not workspace.path.exists()


@dataclass(frozen=True)
class _BrokenTeardown:
    """A backend that raises on the way out, after a perfectly good run."""

    inner: NoSandbox = field(default_factory=NoSandbox)

    async def preflight(self) -> None:
        await self.inner.preflight()

    @asynccontextmanager
    async def start(
        self, ws: Workspace, *, env: Mapping[str, str]
    ) -> AsyncIterator[Sandbox]:
        async with self.inner.start(ws, env=env) as sandbox:
            yield sandbox
        msg = "teardown bug"
        raise RuntimeError(msg)
