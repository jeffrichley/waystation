"""NoSandbox: exec on the host in a cleared environment."""

from __future__ import annotations

import asyncio
import os
import shutil
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from waystation.sandbox.protocol import ExecResult, LineCallback, Sandbox
from waystation.workspace import Workspace

# Minimal host keys required for process startup on Windows / POSIX.
_BASE_PASS_ENV = (
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "COMSPEC",
    "TMP",
    "TEMP",
    "TMPDIR",
    "HOME",
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
)


def _build_env(
    *,
    literal: Mapping[str, str],
    pass_env: Sequence[str],
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    env: dict[str, str] = {}
    for key in (*_BASE_PASS_ENV, *pass_env):
        value = os.environ.get(key)
        if value is not None:
            env[key] = value
    env.update(literal)
    if extra:
        env.update(extra)
    return env


@dataclass(slots=True)
class _HostSandbox:
    workspace: str
    _root: Path
    _env: dict[str, str]

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

        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.workspace,
            env=merged,
        )

        async def _pump(
            stream: asyncio.StreamReader | None,
            callback: LineCallback | None,
            sink: list[str],
        ) -> None:
            if stream is None:
                return
            while True:
                line_b = await stream.readline()
                if not line_b:
                    break
                text = line_b.decode("utf-8", errors="replace")
                if capture or callback is not None:
                    sink.append(text)
                if callback is not None:
                    maybe = callback(text.rstrip("\r\n"))
                    if asyncio.iscoroutine(maybe):
                        await maybe

        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        assert process.stdout is not None
        assert process.stderr is not None
        pump_out = asyncio.create_task(_pump(process.stdout, on_stdout, stdout_parts))
        pump_err = asyncio.create_task(_pump(process.stderr, on_stderr, stderr_parts))

        if stdin is not None and process.stdin is not None:
            process.stdin.write(stdin.encode("utf-8"))
            await process.stdin.drain()
            process.stdin.close()
        elif process.stdin is not None:
            process.stdin.close()

        exit_code = await process.wait()
        await asyncio.gather(pump_out, pump_err)
        return ExecResult(
            exit_code=exit_code,
            stdout="".join(stdout_parts),
            stderr="".join(stderr_parts),
        )


@dataclass(frozen=True, slots=True)
class NoSandbox:
    """Sandbox backend that execs on the host in the workspace temp dir."""

    env: Mapping[str, str] = field(default_factory=dict)
    pass_env: Sequence[str] = ()

    async def preflight(self) -> None:
        return None

    @asynccontextmanager
    async def start(
        self,
        ws: Workspace,
        *,
        env: Mapping[str, str],
        pass_env: Sequence[str],
    ) -> AsyncIterator[Sandbox]:
        merged_literal = {**dict(self.env), **dict(env)}
        merged_pass = (*self.pass_env, *pass_env)
        built = _build_env(literal=merged_literal, pass_env=merged_pass)
        sandbox: Sandbox = _HostSandbox(
            workspace=str(ws.path),
            _root=ws.path,
            _env=built,
        )
        try:
            yield sandbox
        finally:
            shutil.rmtree(ws.path, ignore_errors=True)
