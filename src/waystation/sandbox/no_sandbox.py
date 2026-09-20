"""NoSandbox: exec on the host in a cleared environment."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from waystation.sandbox._host import HostRunner, allowlisted_env, discard_workspace
from waystation.sandbox.processes import ProcessStrategy, host_processes
from waystation.sandbox.protocol import ExecResult, LineCallback, Sandbox
from waystation.workspace import Workspace

__all__ = ["NoSandbox"]


@dataclass(slots=True)
class _HostSandbox:
    workspace: str
    _env: dict[str, str]
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
        merged = dict(self._env)
        if env:
            merged.update(env)
        return await self._runner.run(
            argv,
            stdin=stdin,
            capture=capture,
            on_stdout=on_stdout,
            on_stderr=on_stderr,
            cwd=self.workspace,
            env=merged,
        )


@dataclass(frozen=True, slots=True)
class NoSandbox:
    """Sandbox backend that execs on the host in the workspace temp dir."""

    env: Mapping[str, str] = field(default_factory=dict)
    pass_env: Sequence[str] = ()
    processes: ProcessStrategy = field(default_factory=host_processes)

    async def preflight(self) -> None:
        return None

    @asynccontextmanager
    async def start(
        self,
        ws: Workspace,
        *,
        env: Mapping[str, str],
    ) -> AsyncIterator[Sandbox]:
        # This backend runs host processes, so they need the base keys a
        # process needs to start on this OS; core's literals go on top of
        # this backend's own tier (ADR-0034).
        built = allowlisted_env(
            base=self.processes.base_env_keys(),
            literal={**dict(self.env), **dict(env)},
            pass_env=self.pass_env,
            host_env=os.environ,
        )
        runner = HostRunner(self.processes)
        try:
            yield _HostSandbox(workspace=str(ws.path), _env=built, _runner=runner)
        finally:
            runner.release()
            discard_workspace(ws.path)
