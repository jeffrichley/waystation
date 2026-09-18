"""Any backend that copies calls ``clone_in``; it needs nothing but exec (ADR-0012).

The backend here is the smallest one that copies: an empty host directory,
filled by ``clone_in`` over the protocol's own exec. ``DockerSandbox`` copies
the same way; the docker tier runs it for real.
"""

from __future__ import annotations

import tempfile
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from helpers import a_run, git, sh, subjects, workspaces
from waystation import CommandFailed, NoSandbox, RunFailed, RunSucceeded, Workspace
from waystation.sandbox import ExecResult, Sandbox, clone_in
from waystation.sandbox.protocol import LineCallback
from waystation.workspace import remove_workspace


@dataclass
class _HostSh:
    """A sandbox whose ``sh`` is the host's — Windows keeps Git Bash off PATH."""

    inner: Sandbox
    workspace: str

    async def exec(
        self,
        argv: Sequence[str],
        *,
        stdin: str | None = None,
        env: Mapping[str, str] | None = None,
        capture: bool = True,
        on_stdout: LineCallback | None = None,
        on_stderr: LineCallback | None = None,
    ) -> ExecResult:
        if argv and argv[0] == "sh":
            argv = (sh(), *argv[1:])
        return await self.inner.exec(
            argv,
            stdin=stdin,
            env=env,
            capture=capture,
            on_stdout=on_stdout,
            on_stderr=on_stderr,
        )


@dataclass(frozen=True)
class CopyingHost:
    """Copies each workspace into a fresh directory, then runs there.

    ``leftover`` puts a file in that directory first, the way an image might
    ship a ``/workspace`` that is not empty.
    """

    leftover: bool = False

    async def preflight(self) -> None:
        return None

    @asynccontextmanager
    async def start(
        self, ws: Workspace, *, env: Mapping[str, str], pass_env: Sequence[str]
    ) -> AsyncIterator[Sandbox]:
        inside = Path(tempfile.mkdtemp(prefix="waystation-copy-"))
        if self.leftover:
            (inside / "leftover").write_text("x", encoding="utf-8")
        try:
            async with NoSandbox().start(
                replace(ws, path=inside), env=env, pass_env=pass_env
            ) as sandbox:
                await clone_in(_HostSh(sandbox, sandbox.workspace), ws)
                yield sandbox
        finally:
            remove_workspace(ws.path)


@pytest.mark.git
async def test_a_copied_workspace_runs_like_the_original(
    host_repo: Path, isolated_tempdir: Path
) -> None:
    result = await a_run(host_repo, sandbox=CopyingHost())

    assert isinstance(result, RunSucceeded), result
    assert result.preserved is not None
    assert subjects(host_repo, f"HEAD..{result.preserved}") == ["add a file"]
    assert workspaces(isolated_tempdir) == []


@pytest.mark.git
async def test_a_copied_workspace_commits_as_the_host_identity(
    host_repo: Path,
) -> None:
    result = await a_run(host_repo, sandbox=CopyingHost())

    assert isinstance(result, RunSucceeded), result
    author = git(host_repo, "log", "-1", "--format=%an <%ae>", str(result.preserved))
    assert author == "Waystation Test <test@waystation.example>"


@pytest.mark.git
async def test_a_copy_that_cannot_clone_fails_the_sandbox_stage(
    host_repo: Path, isolated_tempdir: Path
) -> None:
    result = await a_run(host_repo, sandbox=CopyingHost(leftover=True))

    assert isinstance(result, RunFailed), result
    assert result.stage == "sandbox"
    assert isinstance(result.failure, CommandFailed)
    assert "not an empty directory" in result.failure.stderr_tail
    assert workspaces(isolated_tempdir) == []
