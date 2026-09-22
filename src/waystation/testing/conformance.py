"""The sandbox backend contract, as tests rather than as prose (ADR-0035).

``Sandbox.exec`` promises more than a signature can say, and a backend that
keeps six of the seven promises fails in ways that surface far from the
backend — a patch whose bytes were replaced, a line that vanished because it
was long, an agent still writing while its work is collected. So the contract
is stated here, once, and every backend runs it.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest

from waystation import Sandbox, SandboxBackend, Workspace, prepare_workspace

__all__ = ["SandboxConformance"]

# Longer than the stream reader's own limit (64 KiB), so a backend that reads
# a line the easy way loses it and this notices (ADR-0017).
_LONG_LINE = 32 * 2**13
_LONG_LINE_SCRIPT = (
    "s=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx\n"
    "i=0\n"
    'while [ $i -lt 13 ]; do s="$s$s"; i=$((i+1)); done\n'
    'echo "$s"'
)

# More than a pipe buffers, so the write is still going when the process
# exits — the way clone_in's bundle meets a script that refuses to clone.
_MORE_THAN_A_PIPE_HOLDS = "x" * 4_000_000

_MANY_LINES = 500
_MANY_LINES_SCRIPT = (
    f'i=0\nwhile [ $i -lt {_MANY_LINES} ]; do echo "line-$i"; i=$((i+1)); done'
)

# A grandchild that outlives its parent unless the whole tree is killed. It
# pulses rather than runs, so that "it stopped" can be read off a file: the
# interval is short enough that the pause below spans several of them.
_LINGERS = "( while :; do printf x >> pulse; sleep 0.05; done ) & echo started; wait"

# Proving a negative — nothing is still writing — is the one place a wait
# cannot be a poll: there is no signal to poll for. The pause is a multiple
# of the pulse above, not a guess about how fast a machine is.
_PULSE_TWICE = "wc -c < pulse; sleep 0.3; wc -c < pulse"


class SandboxConformance:
    """Every promise a ``SandboxBackend`` makes, for any backend that makes them.

    Point it at your own backend by subclassing it in a module pytest
    collects, under a name starting with ``Test``::

        from waystation.testing import SandboxConformance

        class TestMyBackend(SandboxConformance):
            @pytest.fixture
            def backend(self) -> SandboxBackend:
                return MyBackend(...)

    The suite needs a ``git`` on PATH and, in the sandbox, a POSIX ``sh`` and
    a ``git``, which any sandbox an agent works in has. It runs async tests,
    so it wants ``asyncio_mode = "auto"`` (pytest-asyncio) or the equivalent.

    It asks your sandbox which shell it has rather than being told, so there
    is nothing to override: that is the same ``shell`` a provider's script
    runs under (ADR-0036).
    """

    # ---- what a subclass supplies -------------------------------------

    @pytest.fixture
    def backend(self) -> SandboxBackend:
        """The backend under test. A subclass must override this."""
        msg = "a SandboxConformance subclass provides a `backend` fixture"
        raise NotImplementedError(msg)

    # ---- what the suite builds ----------------------------------------

    @pytest.fixture
    def conformance_repo(self, tmp_path: Path) -> Path:
        """A throwaway host repo: a git identity and one commit on HEAD."""
        repo = tmp_path / "conformance-host"
        repo.mkdir()
        _git(repo, "init")
        _git(repo, "config", "user.name", "Waystation Conformance")
        _git(repo, "config", "user.email", "conformance@waystation.example")
        (repo / "README").write_text("committed\n", encoding="utf-8")
        _git(repo, "add", "README")
        _git(repo, "commit", "-m", "init")
        return repo

    @pytest.fixture
    async def workspace(self, conformance_repo: Path) -> AsyncIterator[Workspace]:
        """One run's workspace, made and removed here as core does (#76)."""
        prepared = await prepare_workspace(conformance_repo)
        try:
            yield prepared
        finally:
            await prepared.remove()

    @pytest.fixture
    async def sandbox(
        self, backend: SandboxBackend, workspace: Workspace
    ) -> AsyncIterator[Sandbox]:
        """The started sandbox, for the tests that do not drive ``start`` itself."""
        async with backend.start(workspace, env={}) as started:
            yield started

    # ---- the contract --------------------------------------------------

    async def test_an_exec_runs_in_the_root_of_this_runs_workspace(
        self, sandbox: Sandbox, workspace: Workspace
    ) -> None:
        """ADR-0043: the working directory is the workspace root, nothing deeper."""
        prefix = await sandbox.exec(["git", "rev-parse", "--show-prefix"])
        branch = await sandbox.exec(["git", "rev-parse", "--abbrev-ref", "HEAD"])

        assert prefix.stdout.strip() == ""
        assert branch.stdout.strip() == workspace.branch

    async def test_the_agent_sees_the_refs_that_travel_and_no_others(
        self, sandbox: Sandbox, workspace: Workspace
    ) -> None:
        """Whichever transport got the workspace here, the repository is the same.

        A copy arrives as a bundle of ``refs``; a bind is the host-side
        workspace itself, which a ``clone --local`` would otherwise have
        filled with the host's other branches, its tags and an ``origin``
        pointing back at it (#76).
        """
        listed = await sandbox.exec(["git", "for-each-ref", "--format=%(refname)"])
        remotes = await sandbox.exec(["git", "remote"])

        assert sorted(listed.stdout.split()) == sorted(workspace.refs)
        assert remotes.stdout.strip() == ""

    async def test_a_sandbox_leaves_the_workspace_where_it_found_it(
        self, backend: SandboxBackend, workspace: Workspace
    ) -> None:
        """Tear down what the backend made, and nothing else.

        Core made the workspace and core removes it, after this context has
        exited. A backend that removes it here is removing a directory
        another backend in the same position never touches, and one a
        composer may still be holding (#76).
        """
        async with backend.start(workspace, env={}) as sandbox:
            assert sandbox.workspace

        assert workspace.path.exists()

    async def test_the_sandbox_says_which_shell_it_has(self, sandbox: Sandbox) -> None:
        """ADR-0036: a caller with a script never has to know the sandbox's OS.

        It is an argv prefix, the shape Dockerfile's ``SHELL`` takes, so a
        sandbox whose shell needs arguments of its own can say so.
        """
        ran = await sandbox.exec([*sandbox.shell, "printf ran"])

        assert list(sandbox.shell), "a sandbox names a shell, however it spells it"
        assert ran.exit_code == 0
        assert ran.stdout == "ran"

    async def test_an_exec_reports_its_exit_code_and_both_its_streams(
        self, sandbox: Sandbox
    ) -> None:
        """A non-zero exit is a result, carrying each stream apart."""
        result = await sandbox.exec([*sandbox.shell, "echo out; echo err >&2; exit 7"])

        assert result.exit_code == 7
        assert result.stdout == "out\n"
        assert result.stderr == "err\n"

    async def test_an_execs_stdin_reaches_the_process(self, sandbox: Sandbox) -> None:
        """What ``stdin`` carries is what the process reads."""
        result = await sandbox.exec([*sandbox.shell, "cat"], stdin="one\ntwo\n")

        assert result.stdout == "one\ntwo\n"

    async def test_an_exec_that_exits_without_reading_its_stdin_reports_its_exit(
        self, sandbox: Sandbox
    ) -> None:
        """A process may exit without reading all it was given.

        Its exit code says why, not the pipe that closed under the write.
        """
        result = await sandbox.exec(
            [*sandbox.shell, "echo refused >&2; exit 3"], stdin=_MORE_THAN_A_PIPE_HOLDS
        )

        assert result.exit_code == 3
        assert result.stderr == "refused\n"

    async def test_captured_output_keeps_a_byte_that_is_not_utf8(
        self, sandbox: Sandbox
    ) -> None:
        """ADR-0030: a patch is not always UTF-8, and git must get its bytes back."""
        lines: list[str] = []

        result = await sandbox.exec(
            [*sandbox.shell, r"printf 'caf\351\n'"], on_stdout=lines.append
        )

        assert result.stdout.encode("utf-8", errors="surrogateescape") == b"caf\xe9\n"
        # What people and parsers read is printable instead (ADR-0030).
        assert lines == ["caf\N{REPLACEMENT CHARACTER}"]

    async def test_a_line_longer_than_a_readers_limit_arrives_whole(
        self, sandbox: Sandbox
    ) -> None:
        """ADR-0017: no line length nobody asked for."""
        lines: list[str] = []

        result = await sandbox.exec(
            [*sandbox.shell, _LONG_LINE_SCRIPT], on_stdout=lines.append
        )

        assert result.stdout == "x" * _LONG_LINE + "\n"
        assert lines == ["x" * _LONG_LINE]

    async def test_output_that_is_not_captured_comes_back_as_a_bounded_tail(
        self, sandbox: Sandbox
    ) -> None:
        """``capture=False`` is how a long agent run stays out of memory."""
        lines: list[str] = []

        result = await sandbox.exec(
            [*sandbox.shell, _MANY_LINES_SCRIPT], capture=False, on_stdout=lines.append
        )

        assert lines == [f"line-{i}" for i in range(_MANY_LINES)]
        assert result.stdout.endswith(f"line-{_MANY_LINES - 1}\n")
        assert "line-0\n" not in result.stdout
        assert len(result.stdout) < len("".join(f"{line}\n" for line in lines))

    @pytest.mark.parametrize("insistent", [False, True])
    async def test_a_cancelled_exec_kills_all_it_started_before_it_completes(
        self, sandbox: Sandbox, insistent: bool
    ) -> None:
        """ADR-0023: nothing the exec started is still writing when collect reads.

        Cancelled again and again meanwhile, the kill still runs to its end.
        """
        started = asyncio.Event()
        running = asyncio.create_task(
            sandbox.exec([*sandbox.shell, _LINGERS], on_stdout=lambda _: started.set())
        )

        await _until(started.is_set, running)
        running.cancel()
        while insistent and not running.done():
            await asyncio.sleep(0.02)
            running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running

        sizes = (await sandbox.exec([*sandbox.shell, _PULSE_TWICE])).stdout.split()
        assert len(sizes) == 2
        assert sizes[0] == sizes[1]

    async def test_a_sandbox_sees_only_the_environment_it_is_given(
        self,
        backend: SandboxBackend,
        workspace: Workspace,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """ADR-0013: cleared and allowlisted — a name nobody wrote never arrives."""
        monkeypatch.setenv("WAYSTATION_CONFORMANCE_UNNAMED", "leaked")
        show = 'printf "%s\\n" "${WAYSTATION_CONFORMANCE-}" '
        show += '"${WAYSTATION_CONFORMANCE_UNNAMED-}"'

        async with backend.start(
            workspace, env={"WAYSTATION_CONFORMANCE": "given"}
        ) as sandbox:
            shown = await sandbox.exec([*sandbox.shell, show])

        assert shown.stdout == "given\n\n"

    async def test_an_execs_own_environment_goes_over_the_sandboxes(
        self, backend: SandboxBackend, workspace: Workspace
    ) -> None:
        """ADR-0034: the more specific tier wins, and an exec's is the most."""
        show = 'printf "%s\\n" "${WAYSTATION_CONFORMANCE-}"'

        async with backend.start(
            workspace, env={"WAYSTATION_CONFORMANCE": "sandbox"}
        ) as sandbox:
            shown = await sandbox.exec(
                [*sandbox.shell, show], env={"WAYSTATION_CONFORMANCE": "exec"}
            )

        assert shown.stdout == "exec\n"


# `_git` and `_until` below are this package's own on purpose: waystation
# ships, and `tests/` does not, so the suite cannot reach `tests/helpers.py`
# for the `init_host_repo` and `until` it mirrors. Change one, look at the
# other — tests/CLAUDE.md says the same from that side.
def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        # A conformance run inherits whatever git config the host has; the
        # repo's own identity is set above, so nothing here needs the host's.
        env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1"},
    )


async def _until(ready: Callable[[], bool], task: asyncio.Task[object]) -> None:
    """Wait until ``ready()`` holds, failing at once if ``task`` ends first.

    It polls, rather than sleeping a guessed interval: a wait long enough on
    one machine is a flake on a slower one.
    """
    while not ready():
        if task.done():
            pytest.fail(f"the exec ended first: {task.result()!r}")
        await asyncio.sleep(0.02)
