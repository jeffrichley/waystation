"""Sandbox protocols and exec results."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from waystation.workspace import Workspace

__all__ = ["ExecResult", "LineCallback", "Sandbox", "SandboxBackend"]

LineCallback = Callable[[str], Awaitable[None] | None]


@dataclass(frozen=True, slots=True)
class ExecResult:
    """How an exec ended: its exit code, and its output or the tails of it."""

    exit_code: int
    stdout: str
    stderr: str


@runtime_checkable
class Sandbox(Protocol):
    """A started sandbox.

    ``workspace`` is the path its execs run in. ``shell`` is the argv a
    command string follows here — ``("sh", "-c")``, or the host's own
    absolute sh for a backend running host processes. A caller with a script
    asks rather than guesses: a path naming a shell on this host means
    nothing inside a container, and a bare ``sh`` is not on a Windows host's
    ``PATH`` (ADR-0029, ADR-0036). It is a prefix, the shape Dockerfile's
    ``SHELL`` takes, so a shell needing arguments of its own can say so;
    ``host_shell()`` answers it for a host backend.

    Writing a backend? The whole of what follows is stated as tests in
    ``waystation.testing.SandboxConformance``, which is what a backend is
    held to — point it at yours (ADR-0035). ``HostRunner`` keeps most of it
    for any backend that drives host processes.
    """

    workspace: str

    @property
    def shell(self) -> Sequence[str]:
        """The argv prefix a command string follows in this sandbox.

        Read only when something wants a shell, so a backend may resolve it
        lazily — a plain attribute satisfies this just as well.
        """
        ...

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
        """Run ``argv`` in the sandbox; each output line to its callback.

        The working directory is the root of ``workspace``, so a caller never
        spells a path that only one backend's host would understand
        (ADR-0043). ``env`` goes over the environment the sandbox started
        with, and nothing else reaches the process (ADR-0013, ADR-0034).

        Captured output is exact: a byte that is not UTF-8 — a latin-1 file
        in a patch, say — stays a surrogate escape (UTF-8 with
        ``surrogateescape``), so git gets it back and the patch lands as
        committed. ``stdin`` is encoded the same way. The callbacks, and the
        tails ``capture=False`` keeps, are for people and parsers: they read
        such a byte as U+FFFD (ADR-0030). A line is whatever length it is;
        none is dropped or split for being long (ADR-0017). ``capture=False``
        is how a long agent run stays out of memory: the result then carries
        bounded tails rather than everything.

        Cancelling it kills the whole tree the exec started, and that is
        done before the cancellation completes — collect must never race a
        process still writing into the workspace (ADR-0023). For a backend
        whose work outlives the host process it spawned, that reaches into
        the sandbox too; ``HostRunner``'s ``on_cancel`` is where that goes.

        Args:
            argv: The command, run directly — no shell unless it names one;
                prefix ``shell`` to run a script.
            stdin: Text fed to the process's stdin, then closed; ``None``
                gives it none. A process that exits without reading it all
                reports its own exit code, not the closed pipe.
            env: Values laid over the sandbox's started environment for this
                exec only.
            capture: Keep all of stdout and stderr in the result. ``False``
                keeps bounded tails instead, for long-running output.
            on_stdout: Called with each stdout line as it arrives; may be a
                coroutine function, which is awaited.
            on_stderr: The same, for stderr.

        Returns:
            The exit code and the captured output, or its tails when
            ``capture`` is ``False``. A non-zero exit is a result, not a
            raise.

        Raises:
            asyncio.CancelledError: When the exec was cancelled — raised only
                once the process tree it started is dead.
        """
        ...


@runtime_checkable
class SandboxBackend(Protocol):
    """A way to isolate a run. ``waystation.testing`` holds it to its contract."""

    async def preflight(self) -> None:
        """Check, before any run starts, that this backend can start one.

        Fan-out calls it once per batch, not once per run (ADR-0032).

        Raises:
            PreflightError: When the backend cannot run here — a daemon not
                reachable, an image missing — saying what to do about it.
        """
        ...

    def start(
        self,
        ws: Workspace,
        *,
        env: Mapping[str, str],
    ) -> AbstractAsyncContextManager[Sandbox]:
        """The run's sandbox, for as long as the context is entered.

        ``env`` is literal values only: core has already resolved the tiers it
        owns, and a backend merges them over its own spec's (ADR-0034). Build
        that one with ``allowlisted_env``; do not read ``os.environ`` by hand.

        ``ws.path`` is not the backend's to remove. Core made it and core
        takes it away, after this context has exited — a copy backend reads
        it once and works in its own container, so a removal here was a duty
        without a reason and a leak whenever one backend forgot (#76). Tear
        down what this backend made, and nothing else.

        A start whose enter fails cleans up after itself, as ``async with``
        expects of any context manager: nothing exits a start that never
        entered.

        Args:
            ws: The workspace the sandbox runs against: bound in, copied in
                with ``clone_in``, or used where it lies.
            env: The literal environment core resolved for this run, laid
                over the backend's own.

        Returns:
            An async context manager whose enter gives the started
            ``Sandbox`` and whose exit tears down what this backend made.
        """
        ...
