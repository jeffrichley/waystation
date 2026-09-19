"""Any backend that copies calls ``clone_in``; it needs nothing but exec (ADR-0012).

The backend here is the smallest one that copies: an empty host directory,
filled by ``clone_in`` over the protocol's own exec. ``DockerSandbox`` copies
the same way; the docker tier runs it for real.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from helpers import a_run, git, sh, subjects, workspaces
from waystation import (
    CommandFailed,
    NoSandbox,
    RunFailed,
    RunSucceeded,
    StageError,
    Workspace,
    prepare_workspace,
)
from waystation.clock import use_clock
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


@dataclass
class _KilledAsItFinished:
    """Its first exec does all its work, then exits 137, as a late SIGKILL would."""

    inner: Sandbox
    workspace: str
    killed: bool = False

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
        result = await self.inner.exec(
            argv,
            stdin=stdin,
            env=env,
            capture=capture,
            on_stdout=on_stdout,
            on_stderr=on_stderr,
        )
        if self.killed:
            return result
        self.killed = True
        return replace(result, exit_code=137)


@dataclass(frozen=True)
class CopyingHost:
    """Copies each workspace into a fresh directory under ``root``, then runs there.

    ``leftover`` puts a file in that directory first, the way an image might
    ship a ``/workspace`` that is not empty. ``killed`` has the copy's first
    try do its work and then exit 137, so the copy runs again over it.
    """

    root: Path
    leftover: bool = False
    killed: bool = False

    async def preflight(self) -> None:
        return None

    @asynccontextmanager
    async def start(
        self, ws: Workspace, *, env: Mapping[str, str], pass_env: Sequence[str]
    ) -> AsyncIterator[Sandbox]:
        inside = self.root / ws.run_id
        inside.mkdir(parents=True)
        if self.leftover:
            (inside / "leftover").write_text("x", encoding="utf-8")
        try:
            async with NoSandbox().start(
                replace(ws, path=inside), env=env, pass_env=pass_env
            ) as sandbox:
                box: Sandbox = _HostSh(sandbox, sandbox.workspace)
                if self.killed:
                    box = _KilledAsItFinished(box, sandbox.workspace)
                await clone_in(box, ws)
                yield sandbox
        finally:
            remove_workspace(ws.path)


@pytest.fixture
def copies(tmp_path: Path) -> Path:
    """Where ``CopyingHost`` makes its copies; empty again once each run ends."""
    return tmp_path / "copies"


@pytest.mark.git
async def test_a_copied_workspace_runs_like_the_original(
    host_repo: Path, isolated_tempdir: Path, copies: Path
) -> None:
    result = await a_run(host_repo, sandbox=CopyingHost(copies))

    assert isinstance(result, RunSucceeded), result
    assert result.preserved is not None
    assert subjects(host_repo, f"HEAD..{result.preserved}") == ["add a file"]
    assert workspaces(isolated_tempdir) == []
    assert list(copies.iterdir()) == []


@pytest.mark.git
async def test_a_copied_workspace_commits_as_the_host_identity(
    host_repo: Path, copies: Path
) -> None:
    result = await a_run(host_repo, sandbox=CopyingHost(copies))

    assert isinstance(result, RunSucceeded), result
    author = git(host_repo, "log", "-1", "--format=%an <%ae>", str(result.preserved))
    assert author == "Waystation Test <test@waystation.example>"


@pytest.mark.git
async def test_a_copy_that_cannot_clone_fails_the_sandbox_stage(
    host_repo: Path, isolated_tempdir: Path, copies: Path
) -> None:
    result = await a_run(host_repo, sandbox=CopyingHost(copies, leftover=True))

    assert isinstance(result, RunFailed), result
    assert result.stage == "sandbox"
    assert isinstance(result.failure, CommandFailed)
    assert "not an empty directory" in result.failure.stderr_tail
    assert workspaces(isolated_tempdir) == []
    assert list(copies.iterdir()) == []


@pytest.mark.git
async def test_a_copy_killed_as_it_finished_runs_again_over_its_own_work(
    host_repo: Path, isolated_tempdir: Path, copies: Path
) -> None:
    result = await a_run(host_repo, sandbox=CopyingHost(copies, killed=True))

    assert isinstance(result, RunSucceeded), result
    assert result.preserved is not None
    assert subjects(host_repo, f"HEAD..{result.preserved}") == ["add a file"]
    assert result.series is not None
    assert not result.series.salvaged  # the second try left nothing stray
    assert workspaces(isolated_tempdir) == []


@dataclass
class _Answers:
    """A sandbox that runs nothing: each exec exits with the next code given.

    ``said`` is shared with ``_Naps``, so the order of tries and waits shows.
    """

    codes: list[int]
    said: list[str | float]
    workspace: str = "/workspace"

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
        self.said.append("exec")
        return ExecResult(exit_code=self.codes.pop(0), stdout="", stderr="")


@dataclass
class _Naps:
    """A clock whose sleeps end at once, saying how long each one was."""

    said: list[str | float] = field(default_factory=list)

    def monotonic(self) -> float:
        return 0.0

    async def sleep(self, seconds: float) -> None:
        self.said.append(seconds)


@pytest.mark.git
@pytest.mark.parametrize(
    ("codes", "tries"),
    [
        ([126, 0], ["exec", 0.25, "exec"]),
        ([137, 0], ["exec", 0.25, "exec"]),
        ([137, 126, 0], ["exec", 0.25, "exec", 0.25, "exec"]),
    ],
)
async def test_a_copy_that_exits_126_or_137_is_tried_again_250_ms_later(
    host_repo: Path, codes: list[int], tries: list[str | float]
) -> None:
    ws = await prepare_workspace(host_repo)
    naps = _Naps()

    with use_clock(naps):
        await clone_in(_Answers(codes, naps.said), ws)

    assert naps.said == tries


@pytest.mark.git
async def test_a_copy_is_tried_again_twice_at_most(host_repo: Path) -> None:
    ws = await prepare_workspace(host_repo)
    naps = _Naps()

    with use_clock(naps), pytest.raises(StageError) as raised:
        await clone_in(_Answers([137, 126, 137, 0], naps.said), ws)

    assert naps.said == ["exec", 0.25, "exec", 0.25, "exec"]
    assert raised.value.stage == "sandbox"
    assert isinstance(raised.value.failure, CommandFailed)
    assert raised.value.failure.exit_code == 137


@pytest.mark.git
@pytest.mark.parametrize("code", [1, 2, 127, 128, 130, 143])
async def test_a_copy_that_fails_any_other_way_is_not_tried_again(
    host_repo: Path, code: int
) -> None:
    ws = await prepare_workspace(host_repo)
    naps = _Naps()

    with use_clock(naps), pytest.raises(StageError) as raised:
        await clone_in(_Answers([code, 0], naps.said), ws)

    assert naps.said == ["exec"]
    assert isinstance(raised.value.failure, CommandFailed)
    assert raised.value.failure.exit_code == code
