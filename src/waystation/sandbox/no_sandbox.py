"""NoSandbox: exec on the host in a cleared environment."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field

from waystation.sandbox.protocol import ExecResult, LineCallback, Sandbox
from waystation.tails import TailBuffer
from waystation.workspace import Workspace

logger = logging.getLogger("waystation")

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


def _kill_process_tree(
    process: asyncio.subprocess.Process,
    *,
    job: object | None = None,
) -> None:
    """Kill ``process`` and every descendant before cancellation completes."""
    pid = process.pid
    if pid is None and job is None:
        return
    if sys.platform == "win32":
        if job is not None:
            with suppress(OSError):
                import ctypes

                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel32.TerminateJobObject(job, 1)
        if pid is not None:
            import subprocess as sp

            with suppress(OSError):
                sp.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    check=False,
                    capture_output=True,
                )
            with suppress(ProcessLookupError, OSError):
                process.kill()
        return
    # POSIX: started in its own session / process group.
    if pid is not None:  # pragma: no cover
        with suppress(ProcessLookupError, OSError, PermissionError):
            os.killpg(pid, signal.SIGKILL)
        with suppress(ProcessLookupError, OSError):
            process.kill()


@dataclass(slots=True)
class _HostSandbox:
    workspace: str
    _env: dict[str, str]
    _job: object | None = None

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

        popen_kwargs: dict[str, object] = {
            "stdin": asyncio.subprocess.PIPE,
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
            "cwd": self.workspace,
            "env": merged,
        }
        if sys.platform != "win32":  # pragma: no cover
            popen_kwargs["start_new_session"] = True
        else:
            # CREATE_NEW_PROCESS_GROUP; Job Object + taskkill /T on cancel.
            popen_kwargs["creationflags"] = getattr(
                __import__("subprocess"), "CREATE_NEW_PROCESS_GROUP", 0
            )

        process = await asyncio.create_subprocess_exec(*argv, **popen_kwargs)  # type: ignore[arg-type]

        if sys.platform == "win32":
            self._assign_windows_job(process)

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
            _kill_process_tree(process, job=self._job)
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

    def _assign_windows_job(self, process: asyncio.subprocess.Process) -> None:
        """Place the process in a Job Object so tree kill is reliable."""
        if process.pid is None:
            return
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
            ProcessExtendLimitInformation = 9

            class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
                _fields_ = [
                    ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
                    ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD),
                ]

            class IO_COUNTERS(ctypes.Structure):
                _fields_ = [
                    ("ReadOperationCount", ctypes.c_ulonglong),
                    ("WriteOperationCount", ctypes.c_ulonglong),
                    ("OtherOperationCount", ctypes.c_ulonglong),
                    ("ReadTransferCount", ctypes.c_ulonglong),
                    ("WriteTransferCount", ctypes.c_ulonglong),
                    ("OtherTransferCount", ctypes.c_ulonglong),
                ]

            class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
                _fields_ = [
                    ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                    ("IoInfo", IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t),
                ]

            job = kernel32.CreateJobObjectW(None, None)
            if not job:
                return
            info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not kernel32.SetInformationJobObject(
                job,
                ProcessExtendLimitInformation,
                ctypes.byref(info),
                ctypes.sizeof(info),
            ):
                kernel32.CloseHandle(job)
                return
            PROCESS_ALL_ACCESS = 0x1F0FFF
            handle = kernel32.OpenProcess(PROCESS_ALL_ACCESS, False, process.pid)
            if not handle:
                kernel32.CloseHandle(job)
                return
            ok = kernel32.AssignProcessToJobObject(job, handle)
            kernel32.CloseHandle(handle)
            if not ok:
                kernel32.CloseHandle(job)
                return
            self._job = job
        except OSError:
            logger.exception("failed to assign process to Windows Job Object")


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
        import shutil

        merged_literal = {**dict(self.env), **dict(env)}
        merged_pass = (*self.pass_env, *pass_env)
        built = _build_env(literal=merged_literal, pass_env=merged_pass)
        sandbox = _HostSandbox(
            workspace=str(ws.path),
            _env=built,
        )
        try:
            yield sandbox
        finally:
            if sandbox._job is not None and sys.platform == "win32":
                with suppress(OSError):
                    import ctypes

                    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                    kernel32.TerminateJobObject(sandbox._job, 1)
                    kernel32.CloseHandle(sandbox._job)
            try:
                shutil.rmtree(ws.path)
            except OSError:
                logger.exception(
                    "teardown failed while removing workspace %s", ws.path
                )
