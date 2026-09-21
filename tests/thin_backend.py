"""A third sandbox backend, built from waystation's public surface alone.

It is alone in this module so a test can read its imports and fail if one of
them is private: that is what proves a backend waystation does not ship can
keep the whole exec contract without reaching inside (#75, ADR-0035).

Nothing here is clever. It is the thin adapter a host-process backend is
meant to be, and the suite it passes is the one both shipped backends run.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from waystation import ExecResult, Sandbox, Workspace
from waystation.sandbox import (
    HostRunner,
    LineCallback,
    ProcessStrategy,
    allowlisted_env,
    host_processes,
    host_shell,
)

__all__ = ["ThinHost"]


@dataclass(slots=True)
class _ThinSandbox:
    workspace: str
    shell: Sequence[str]
    _env: Mapping[str, str]
    _runner: HostRunner

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
        return await self._runner.run(
            argv,
            stdin=stdin,
            capture=capture,
            on_stdout=on_stdout,
            on_stderr=on_stderr,
            cwd=self.workspace,
            env={**self._env, **(env or {})},
        )


@dataclass(frozen=True, slots=True)
class ThinHost:
    """Execs the workspace's own host processes, isolating nothing."""

    processes: ProcessStrategy = field(default_factory=host_processes)

    async def preflight(self) -> None:
        return None

    @asynccontextmanager
    async def start(
        self, ws: Workspace, *, env: Mapping[str, str]
    ) -> AsyncIterator[Sandbox]:
        built = allowlisted_env(
            base=self.processes.base_env_keys(),
            literal=env,
            pass_env=(),
            host_env=os.environ,
        )
        # Nothing removes `ws`: core made it and core takes it away, so a
        # backend this thin has nothing to tear down but its own runner (#76).
        with HostRunner(self.processes) as runner:
            yield _ThinSandbox(
                workspace=str(ws.path),
                shell=host_shell(),
                _env=built,
                _runner=runner,
            )
