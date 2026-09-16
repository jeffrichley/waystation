"""Sandbox protocols and exec results."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from waystation.workspace import Workspace

LineCallback = Callable[[str], Awaitable[None] | None]


@dataclass(frozen=True, slots=True)
class ExecResult:
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
    ) -> ExecResult: ...


@runtime_checkable
class SandboxBackend(Protocol):
    async def preflight(self) -> None: ...

    def start(
        self,
        ws: Workspace,
        *,
        env: Mapping[str, str],
        pass_env: Sequence[str],
    ) -> AbstractAsyncContextManager[Sandbox]: ...
