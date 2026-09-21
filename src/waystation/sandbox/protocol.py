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
    """A started sandbox. ``workspace`` is the path its execs run in.

    Writing a backend? The whole of what follows is stated as tests in
    ``waystation.testing.SandboxConformance``, which is what a backend is
    held to — point it at yours (ADR-0035). ``HostRunner`` keeps most of it
    for any backend that drives host processes.
    """

    workspace: str
    shell: Sequence[str]
    """Argv a shell command string follows here — ``("sh", "-c")``, or the
    host's own absolute sh for a backend that runs host processes.

    A caller with a script to run asks rather than guesses: a path that names
    a shell on this host means nothing inside a container, and a bare ``sh``
    is not on a Windows host's ``PATH`` (ADR-0029, ADR-0036). It is a prefix,
    the shape Dockerfile's ``SHELL`` takes, so a shell needing arguments of
    its own can say so. ``host_shell()`` answers it for a host backend.
    """

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
        (ADR-0012). ``env`` goes over the environment the sandbox started
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
        """
        ...


@runtime_checkable
class SandboxBackend(Protocol):
    """A way to isolate a run. ``waystation.testing`` holds it to its contract."""

    async def preflight(self) -> None: ...

    def start(
        self,
        ws: Workspace,
        *,
        env: Mapping[str, str],
    ) -> AbstractAsyncContextManager[Sandbox]:
        """The run's sandbox, which owns ``ws`` from here and removes it on exit.

        ``env`` is literal values only: core has already resolved the tiers it
        owns, and a backend merges them over its own spec's (ADR-0034). Build
        that one with ``allowlisted_env``; do not read ``os.environ`` by hand.

        Removing ``ws.path`` on exit is the backend's, however it got the
        workspace in: ``discard_workspace`` is that removal, git's read-only
        objects and Windows' hold on them included.

        A start whose enter fails cleans up after itself, as ``async with``
        expects of any context manager: nothing exits a start that never
        entered.
        """
        ...
