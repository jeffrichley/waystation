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
        """Run ``argv`` in the sandbox; each output line to its callback.

        Captured output is exact: a byte that is not UTF-8 — a latin-1 file
        in a patch, say — stays a surrogate escape (UTF-8 with
        ``surrogateescape``), so git gets it back and the patch lands as
        committed. ``stdin`` is encoded the same way. The callbacks, and the
        tails ``capture=False`` keeps, are for people and parsers: they read
        such a byte as U+FFFD (ADR-0030).
        """
        ...


@runtime_checkable
class SandboxBackend(Protocol):
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

        A start whose enter fails cleans up after itself, as ``async with``
        expects of any context manager: nothing exits a start that never
        entered.
        """
        ...
