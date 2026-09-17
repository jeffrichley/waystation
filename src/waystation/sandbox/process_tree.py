"""Process-tree strategies: how a host kills an exec and everything it started.

ADR-0023 promises that cancelling an exec kills its whole process tree. Each
operating system does that differently, so each way is a strategy that
``NoSandbox`` takes by injection. The host's own strategy is the default, and
``host_process_trees`` is the only place that chooses by platform; the guards
inside each strategy only refuse to run on the wrong one.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
import sys
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger("waystation")


@runtime_checkable
class ProcessTree(Protocol):
    """One spawned process and every descendant it starts."""

    def kill(self) -> None:
        """Kill the whole tree now; harmless once it has already exited."""
        ...

    def release(self) -> None:
        """The sandbox is going away; let go of anything the tree holds."""
        ...


@runtime_checkable
class ProcessTreeStrategy(Protocol):
    """How to start processes so that each one's whole tree can be killed."""

    def spawn_options(self) -> dict[str, Any]:
        """Extra ``asyncio.create_subprocess_exec`` keyword arguments."""
        ...

    def adopt(self, process: asyncio.subprocess.Process) -> ProcessTree:
        """Take charge of a process just started with ``spawn_options``."""
        ...


@dataclass(frozen=True, slots=True)
class PosixProcessGroups:
    """Each exec leads its own session, so killing its process group kills the tree."""

    def spawn_options(self) -> dict[str, Any]:
        return {"start_new_session": True}

    def adopt(self, process: asyncio.subprocess.Process) -> ProcessTree:
        return _ProcessGroup(process)


@dataclass(slots=True)
class _ProcessGroup:
    process: asyncio.subprocess.Process

    def kill(self) -> None:
        if sys.platform == "win32":
            raise RuntimeError("PosixProcessGroups needs a POSIX host")
        with suppress(OSError):
            os.killpg(self.process.pid, signal.SIGKILL)
        with suppress(OSError):
            self.process.kill()

    def release(self) -> None:
        # A group id is reused once its leader is gone, so a group is only
        # killed while its exec is still being awaited, never at teardown.
        return None


@dataclass(frozen=True, slots=True)
class WindowsJobObjects:
    """Each exec joins its own Job Object, so terminating the job kills the tree."""

    def spawn_options(self) -> dict[str, Any]:
        if sys.platform != "win32":
            raise RuntimeError("WindowsJobObjects needs a Windows host")
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}

    def adopt(self, process: asyncio.subprocess.Process) -> ProcessTree:
        return _JobObject(process, _job_for(process))


@dataclass(slots=True)
class _JobObject:
    process: asyncio.subprocess.Process
    job: int | None

    def kill(self) -> None:
        if self.job is not None:
            with suppress(OSError):
                _kernel32().TerminateJobObject(self.job, 1)
        # taskkill /T also reaches children started before the job existed.
        with suppress(OSError):
            subprocess.run(
                ["taskkill", "/PID", str(self.process.pid), "/T", "/F"],
                check=False,
                capture_output=True,
            )
        with suppress(ProcessLookupError, OSError):
            self.process.kill()

    def release(self) -> None:
        if self.job is None:
            return
        with suppress(OSError):
            kernel32 = _kernel32()
            kernel32.TerminateJobObject(self.job, 1)
            kernel32.CloseHandle(self.job)
        self.job = None


def host_process_trees() -> ProcessTreeStrategy:
    """The strategy for the operating system this process is running on."""
    if sys.platform == "win32":
        return WindowsJobObjects()
    return PosixProcessGroups()


def _kernel32() -> Any:
    if sys.platform != "win32":
        raise RuntimeError("kernel32 exists only on Windows")
    import ctypes

    return ctypes.WinDLL("kernel32", use_last_error=True)


def _job_for(process: asyncio.subprocess.Process) -> int | None:
    """Put ``process`` in a new kill-on-close Job Object; ``None`` if that fails."""
    import ctypes
    from ctypes import wintypes

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

    job_object_limit_kill_on_job_close = 0x00002000
    process_extend_limit_information = 9
    process_all_access = 0x1F0FFF
    try:
        kernel32 = _kernel32()
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = job_object_limit_kill_on_job_close
        if not kernel32.SetInformationJobObject(
            job,
            process_extend_limit_information,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            kernel32.CloseHandle(job)
            return None
        handle = kernel32.OpenProcess(process_all_access, False, process.pid)
        if not handle:
            kernel32.CloseHandle(job)
            return None
        joined = kernel32.AssignProcessToJobObject(job, handle)
        kernel32.CloseHandle(handle)
        if not joined:
            kernel32.CloseHandle(job)
            return None
        return int(job)
    except OSError:
        logger.exception("failed to assign process to a Windows Job Object")
        return None
