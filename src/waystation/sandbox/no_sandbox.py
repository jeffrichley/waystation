"""NoSandbox: exec on the host in a cleared environment."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field

from waystation.observability import get_logger, redact_argv
from waystation.sandbox.processes import (
    ProcessStrategy,
    ProcessTree,
    host_processes,
)
from waystation.sandbox.protocol import ExecResult, LineCallback, Sandbox
from waystation.tails import TailBuffer
from waystation.workspace import Workspace, remove_workspace

logger = get_logger("waystation.sandbox")


def _build_env(
    *,
    base: Sequence[str],
    literal: Mapping[str, str],
    pass_env: Sequence[str],
) -> dict[str, str]:
    env: dict[str, str] = {}
    for key in (*base, *pass_env):
        value = os.environ.get(key)
        if value is not None:
            env[key] = value
    env.update(literal)
    return env


def _rmtree_retry(path: str | os.PathLike[str], *, attempts: int = 5) -> None:
    """Remove a workspace dir; retry briefly on Windows file-lock races."""
    import time

    last: OSError | None = None
    for i in range(attempts):
        try:
            remove_workspace(path)
            return
        except OSError as exc:
            last = exc
            time.sleep(0.05 * (i + 1))
    logger.error("teardown failed while removing workspace %s: %s", path, last)


@dataclass(slots=True)
class _HostSandbox:
    workspace: str
    _env: dict[str, str]
    _strategy: ProcessStrategy
    _trees: list[ProcessTree] = field(default_factory=list)

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
        logger.debug("exec: %s", " ".join(redact_argv(argv)))

        popen_kwargs: dict[str, object] = {
            "stdin": asyncio.subprocess.PIPE,
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
            "cwd": self.workspace,
            "env": merged,
            **self._strategy.spawn_options(),
        }
        process = await asyncio.create_subprocess_exec(*argv, **popen_kwargs)  # type: ignore[arg-type]
        tree = self._strategy.adopt(process)
        self._trees.append(tree)

        async def _pump(
            stream: asyncio.StreamReader | None,
            callback: LineCallback | None,
            full: list[str] | None,
            tail: TailBuffer | None,
        ) -> None:
            if stream is None:
                return
            while True:
                line_b = await stream.readline()
                if not line_b:
                    break
                text = line_b.decode("utf-8", errors="replace")
                if full is not None:
                    full.append(text)
                if tail is not None:
                    tail.append(text)
                if callback is not None:
                    maybe = callback(text.rstrip("\r\n"))
                    if asyncio.iscoroutine(maybe):
                        await maybe

        stdout_full: list[str] | None = [] if capture else None
        stderr_full: list[str] | None = [] if capture else None
        stdout_tail = None if capture else TailBuffer()
        stderr_tail = None if capture else TailBuffer()
        assert process.stdout is not None
        assert process.stderr is not None
        pump_out = asyncio.create_task(
            _pump(process.stdout, on_stdout, stdout_full, stdout_tail)
        )
        pump_err = asyncio.create_task(
            _pump(process.stderr, on_stderr, stderr_full, stderr_tail)
        )

        if stdin is not None and process.stdin is not None:
            process.stdin.write(stdin.encode("utf-8"))
            await process.stdin.drain()
            process.stdin.close()
        elif process.stdin is not None:
            process.stdin.close()

        try:
            exit_code = await process.wait()
            await asyncio.gather(pump_out, pump_err)
        except asyncio.CancelledError:
            tree.kill()
            with suppress(asyncio.CancelledError):
                await process.wait()
            pump_out.cancel()
            pump_err.cancel()
            with suppress(asyncio.CancelledError):
                await asyncio.gather(pump_out, pump_err)
            raise

        if capture:
            assert stdout_full is not None
            assert stderr_full is not None
            return ExecResult(
                exit_code=exit_code,
                stdout="".join(stdout_full),
                stderr="".join(stderr_full),
            )
        assert stdout_tail is not None
        assert stderr_tail is not None
        return ExecResult(
            exit_code=exit_code,
            stdout=stdout_tail.text(),
            stderr=stderr_tail.text(),
        )

    def release_trees(self) -> None:
        for tree in self._trees:
            tree.release()
        self._trees.clear()


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
        pass_env: Sequence[str],
    ) -> AsyncIterator[Sandbox]:
        merged_literal = {**dict(self.env), **dict(env)}
        merged_pass = (*self.pass_env, *pass_env)
        built = _build_env(
            base=self.processes.base_env_keys(),
            literal=merged_literal,
            pass_env=merged_pass,
        )
        sandbox = _HostSandbox(
            workspace=str(ws.path),
            _env=built,
            _strategy=self.processes,
        )
        try:
            yield sandbox
        finally:
            sandbox.release_trees()
            _rmtree_retry(ws.path)
