"""What every sandbox backend does on the host, whatever it isolates with.

``NoSandbox`` runs the agent here; ``DockerSandbox`` runs the ``docker`` client
here. Either way one host process is spawned, its two streams are read a line
at a time, and its tree is killed if the exec is cancelled (ADR-0023) — so
there is one runner, not one per backend (ADR-0010).
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field

from waystation._git import decode, encode
from waystation.observability import SANDBOX, log_argv
from waystation.sandbox.processes import ProcessStrategy, ProcessTree
from waystation.sandbox.protocol import ExecResult, LineCallback
from waystation.tails import TailBuffer
from waystation.workspace import remove_workspace

__all__ = ["HostRunner", "allowlisted_env", "discard_workspace"]


def allowlisted_env(
    *,
    literal: Mapping[str, str],
    pass_env: Sequence[str],
    host_env: Mapping[str, str],
    base: Sequence[str] = (),
) -> dict[str, str]:
    """The environment a sandbox or an exec gets: only what was named (ADR-0013).

    The one place an environment is built, for core and for a backend alike
    (ADR-0034). Core resolves the tiers it owns — the agent provider's, and
    the per-run ones — and hands the result on as literals; a backend
    resolves its own spec's, plus the ``base`` keys its execs need on this OS.

    ``base`` and ``pass_env`` names are looked up in ``host_env``, and skipped
    where it has none, so ``pass_env=("CI",)`` stays usable on a laptop; the
    skipped *names* are logged at DEBUG, never a value (ADR-0025). A
    ``literal`` wins over a name in the same call: it is the most explicit
    thing a user wrote, where a pass-through name only says "bring whatever
    the host has".

    ``host_env`` is passed in rather than read, so the caller decides when
    ``os.environ`` is read and how far one reading stretches: core reads once
    for the tiers it owns, and a backend once for its own (ADR-0034).
    """
    env: dict[str, str] = {}
    skipped: list[str] = []
    for key in (*base, *pass_env):
        value = host_env.get(key)
        if value is not None:
            env[key] = value
        else:
            skipped.append(key)
    env.update(literal)
    if skipped:
        SANDBOX.debug("pass_env names not on the host, skipped: %s", ", ".join(skipped))
    return env


def discard_workspace(path: str | os.PathLike[str], *, attempts: int = 5) -> None:
    """Remove a workspace dir; retry briefly on Windows file-lock races.

    A teardown failure is logged, never raised: it must not change a result
    that is already decided (ADR-0016).
    """
    last: OSError | None = None
    for i in range(attempts):
        try:
            remove_workspace(path)
            return
        except OSError as exc:
            last = exc
            time.sleep(0.05 * (i + 1))
    SANDBOX.error("teardown failed while removing workspace %s: %s", path, last)


async def _read_line(stream: asyncio.StreamReader) -> bytes:
    """The next line, however long; ``b""`` at EOF.

    ``readline`` raises on a line past the reader's limit (64 KiB by default)
    and drops it. ``readuntil`` leaves an overrun buffered, so the line is
    taken a limit's worth at a time and none is too long (ADR-0017).
    """
    parts: list[bytes] = []
    while True:
        try:
            parts.append(await stream.readuntil(b"\n"))
            break
        except asyncio.LimitOverrunError as exc:
            parts.append(await stream.readexactly(exc.consumed))
        except asyncio.IncompleteReadError as exc:
            parts.append(exc.partial)
            break
    return b"".join(parts)


@dataclass(slots=True)
class HostRunner:
    """Runs one sandbox's host processes, and lets their trees go with it."""

    processes: ProcessStrategy
    _trees: list[ProcessTree] = field(default_factory=list)

    async def run(
        self,
        argv: Sequence[str],
        *,
        stdin: str | None = None,
        capture: bool = True,
        on_stdout: LineCallback | None = None,
        on_stderr: LineCallback | None = None,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> ExecResult:
        """Run ``argv`` on the host: stdin in, both streams out a line at a time.

        Cancelling it kills the process's tree first (ADR-0023); a tree still
        held when the sandbox goes is let go by ``release``. ``capture=False``
        keeps only the tails. ``env=None`` inherits the host's. Captured
        output keeps every byte; callbacks and tails read one that is not
        UTF-8 as U+FFFD, as ``Sandbox.exec`` promises (ADR-0030).
        """
        log_argv(SANDBOX, argv)
        popen_kwargs: dict[str, object] = {
            "stdin": asyncio.subprocess.PIPE,
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
            "cwd": cwd,
            "env": None if env is None else dict(env),
            **self.processes.spawn_options(),
        }
        process = await asyncio.create_subprocess_exec(*argv, **popen_kwargs)  # type: ignore[arg-type]
        tree = self.processes.adopt(process)
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
                line_b = await _read_line(stream)
                if not line_b:
                    break
                if full is not None:
                    # Exact, for git: a patch cut from it lands byte for byte.
                    full.append(decode(line_b))
                # Readable, for people and parsers: a lone surrogate breaks a
                # print, a UTF-8 log, or an Outcome dumped to JSON.
                text = line_b.decode("utf-8", errors="replace")
                if tail is not None:
                    tail.append(text)
                if callback is None:
                    # Nobody asked to watch this exec, so it is setup rather
                    # than the agent: its output belongs at DEBUG (issue #6).
                    # The agent's lines are logged by whoever asked for them.
                    SANDBOX.debug("%s", text.rstrip("\r\n"))
                    continue
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

        try:
            # Inside the try: a cancellation mid-write kills the tree like any other.
            if process.stdin is not None:
                if stdin is not None:
                    # A process may exit without reading all it was given; its
                    # exit code says why, not the pipe that closed under the write.
                    with suppress(BrokenPipeError, ConnectionResetError):
                        process.stdin.write(encode(stdin))
                        await process.stdin.drain()
                process.stdin.close()
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

    def release(self) -> None:
        """The sandbox is going away: let go of every tree its execs started."""
        for tree in self._trees:
            tree.release()
        self._trees.clear()
