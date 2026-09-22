"""Process strategies: everything OS-specific about running host processes.

Which environment a process needs to start, how to start it, and how to kill
it with everything it spawned (ADR-0023) all differ by operating system, so
each way is one strategy that ``NoSandbox`` takes by injection, and host git
runs through the host's own (ADR-0027). The host's own strategy is the
default, and ``host_processes`` is the only place that chooses by platform;
the guards inside each strategy only refuse the wrong one.

Signals (``handle_signals``) and the Docker transport default (ADR-0043) are
OS-specific too, but they belong to their own consumers, not here.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from waystation.observability import package_logger

__all__ = [
    "PosixProcesses",
    "ProcessStrategy",
    "ProcessTree",
    "WindowsProcesses",
    "host_processes",
]

# Keys a process needs to start at all, per OS; a run's own env goes on top.
_POSIX_BASE_ENV = ("PATH", "HOME", "TMPDIR")
_WINDOWS_BASE_ENV = (
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "COMSPEC",
    "TMP",
    "TEMP",
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
)

_logger = package_logger()

# A child created suspended cannot spawn anything before it is in its job.
# `subprocess` has no name for this flag; the value is CREATE_SUSPENDED from
# the Windows process-creation flags.
_CREATE_SUSPENDED = 0x00000004


@runtime_checkable
class ProcessTree(Protocol):
    """One spawned process and every descendant it starts."""

    def kill(self) -> None:
        """Kill the whole tree now; harmless once it has already exited.

        Called more than once per tree, and each call must be safe: the
        runner sweeps again a beat after the first, for a child that was
        being forked as the first sweep went past it (ADR-0046).
        """
        ...

    def release(self) -> None:
        """The sandbox is going away; let go of anything the tree holds."""
        ...


@runtime_checkable
class ProcessStrategy(Protocol):
    """How one operating system starts, and stops, a sandbox's processes."""

    def base_env_keys(self) -> Sequence[str]:
        """Host env keys a process needs to start on this OS.

        Returns:
            The names ``allowlisted_env`` passes through from the host before
            anything else; a run's own env goes on top.
        """
        ...

    def spawn_options(self) -> dict[str, Any]:
        """Extra ``asyncio.create_subprocess_exec`` keyword arguments.

        Returns:
            What makes a process started with them one ``adopt`` can take
            charge of as a tree — its own session, say, or suspended.
        """
        ...

    def adopt(self, process: asyncio.subprocess.Process) -> ProcessTree:
        """Take charge of a process just started with ``spawn_options``.

        Args:
            process: The process just spawned, before anything has awaited it.

        Returns:
            The tree it leads, which kills every descendant it started, not
            only the process itself (ADR-0023).
        """
        ...


@dataclass(frozen=True, slots=True)
class PosixProcesses:
    """Each exec leads its own session, so killing its process group kills the tree."""

    def base_env_keys(self) -> Sequence[str]:
        """Host env keys a process needs to start on a POSIX host.

        Returns:
            ``PATH``, ``HOME`` and ``TMPDIR``.
        """
        return _POSIX_BASE_ENV

    def spawn_options(self) -> dict[str, Any]:
        """Start each exec as the leader of a new session.

        Returns:
            ``start_new_session=True``.
        """
        return {"start_new_session": True}

    def adopt(self, process: asyncio.subprocess.Process) -> ProcessTree:
        """Take charge of the process group ``process`` leads.

        Args:
            process: A process started with ``spawn_options``.

        Returns:
            Its process group, killed with ``SIGKILL``. Killing it on a
            Windows host raises ``RuntimeError``.
        """
        return _ProcessGroup(process)


@dataclass(slots=True)
class _ProcessGroup:
    process: asyncio.subprocess.Process

    def kill(self) -> None:
        """Freeze the group, then sweep it; safe to call again (#110).

        ``kill(-pgid)`` walks the process table, and the walk is not atomic
        against a fork: a child born as it passes inherits the group with
        the signal already spent, then outlives its killed parent as an
        orphan of init. Freezing first is what makes a second sweep enough
        rather than merely likely — a stopped member cannot *start* another
        fork, so all that can still arrive is what was already in flight.
        ``SIGSTOP``, like ``SIGKILL``, is the kernel's to deliver and not a
        process's to catch, and a stopped process takes a ``SIGKILL`` as it
        is (jmmv, "How to kill a tree of processes").

        The runner sweeps again a beat later, while the leader is still
        unreaped so the group id is still this group's (ADR-0046).
        """
        if sys.platform == "win32":
            raise RuntimeError("PosixProcesses needs a POSIX host")
        with suppress(OSError):
            os.killpg(self.process.pid, signal.SIGSTOP)
        with suppress(OSError):
            os.killpg(self.process.pid, signal.SIGKILL)
        with suppress(OSError):
            self.process.kill()

    def release(self) -> None:
        # A group id is reused once its leader is gone, so a group is only
        # killed while its exec is still being awaited, never at teardown.
        return None


@dataclass(frozen=True, slots=True)
class WindowsProcesses:
    """Each exec joins its own Job Object, so terminating the job kills the tree."""

    def base_env_keys(self) -> Sequence[str]:
        """Host env keys a process needs to start on a Windows host.

        Returns:
            ``PATH``, ``SYSTEMROOT``, ``TEMP`` and the rest a Windows process
            reads before it runs any code of its own.
        """
        return _WINDOWS_BASE_ENV

    def spawn_options(self) -> dict[str, Any]:
        """Start each exec in its own process group, suspended.

        Returns:
            The ``creationflags`` that do both.

        Raises:
            RuntimeError: On a host that is not Windows.
        """
        if sys.platform != "win32":
            raise RuntimeError("WindowsProcesses needs a Windows host")
        # Suspended, so it cannot spawn anything before it is in the job: see
        # `adopt`, which is where the reason lives (#105).
        return {
            "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | _CREATE_SUSPENDED
        }

    def adopt(self, process: asyncio.subprocess.Process) -> ProcessTree:
        """Put the process in its job, then let it run — in that order (#105).

        The order is the whole point. ``AssignProcessToJobObject`` adds one
        process, never the descendants it already has, and asyncio's spawn
        takes long enough under load — 150 ms to 3 s was measured — for a
        shell to have forked several. Those are outside the job, so they
        survive its kill, still holding the stdout they inherited; and
        ``Process.wait()`` on Windows waits for every pipe to close rather
        than for the process to exit, so one of them wedges a cancelled run
        for ever (cpython gh-119710, present on 3.12, 3.13 and 3.14 alike).
        Creating suspended closes that window: nothing has run, so there is
        nothing to leave behind.

        Args:
            process: A process started with ``spawn_options``, still
                suspended.

        Returns:
            Its job, whose termination kills the tree. A host that gave no
            job falls back to killing the process alone.

        Raises:
            OSError: When the process cannot be opened or resumed: one left
                suspended would never exit, so it is better failed here.
            RuntimeError: On a host that is not Windows.
        """
        handle = _handle_of(process)
        job = None
        try:
            job = _job_for(handle)
        finally:
            # Whatever the job did, the process must not be left stopped:
            # a suspended process nobody resumes is one nothing collects.
            _resume(handle)
        return _JobObject(process, job)


@dataclass(slots=True)
class _JobObject:
    process: asyncio.subprocess.Process
    job: int | None

    def kill(self) -> None:
        """Terminate the job, which is every process the exec started.

        No ``taskkill``: it walks live parent-child links at snapshot time,
        so it misses exactly the orphan a job is for. It also blocked the
        event loop for half a second per exec, to report "process not found"
        on all 129 kills of a measured run (#105). ``process.kill`` stays as
        the fallback for a host that gave us no job.
        """
        if self.job is not None:
            with suppress(OSError):
                _kernel32().TerminateJobObject(self.job, 1)
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


def host_processes() -> ProcessStrategy:
    """The strategy for the operating system this process is running on.

    Returns:
        ``WindowsProcesses`` on Windows, ``PosixProcesses`` anywhere else.
    """
    if sys.platform == "win32":
        return WindowsProcesses()
    return PosixProcesses()


def _kernel32() -> Any:
    """kernel32 with every prototype declared.

    Without ``restype`` ctypes hands back a C ``int``, which truncates a
    64-bit ``HANDLE`` and turns one with the top bit set negative; without
    ``argtypes`` it marshals each argument as a 32-bit int. Handle values are
    small enough that this worked, which is what makes it worth declaring —
    the failure would be silent and would arrive on someone else's machine.
    A mismatch also raises ``ctypes.ArgumentError``, which is not an
    ``OSError``, so it would walk straight out through a ``suppress(OSError)``
    (#105).
    """
    if sys.platform != "win32":
        raise RuntimeError("kernel32 exists only on Windows")
    import ctypes
    from ctypes import wintypes

    dll = ctypes.WinDLL("kernel32", use_last_error=True)
    dll.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    dll.CreateJobObjectW.restype = wintypes.HANDLE
    dll.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    dll.SetInformationJobObject.restype = wintypes.BOOL
    dll.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    dll.AssignProcessToJobObject.restype = wintypes.BOOL
    dll.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    dll.TerminateJobObject.restype = wintypes.BOOL
    dll.CloseHandle.argtypes = [wintypes.HANDLE]
    dll.CloseHandle.restype = wintypes.BOOL
    dll.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    dll.OpenProcess.restype = wintypes.HANDLE
    return dll


def _handle_of(process: asyncio.subprocess.Process) -> int:
    """The process handle asyncio already holds, not a fresh one by pid.

    ``OpenProcess(pid)`` is a race: a pid identifies a process only while
    something holds a handle to it, and reopening one asyncio is already
    holding buys nothing but the chance of opening someone else's (#105).
    The transport is private, so a host whose asyncio has moved it falls
    back — safely, because the handle asyncio holds is what pins the pid.
    """
    transport = getattr(process, "_transport", None)
    popen = None if transport is None else transport.get_extra_info("subprocess")
    handle = getattr(popen, "_handle", None)
    if handle:
        return int(handle)
    process_set_quota_and_terminate = 0x0100 | 0x0001
    opened = _kernel32().OpenProcess(
        process_set_quota_and_terminate, False, process.pid
    )
    if not opened:
        msg = f"could not open the process just started (pid {process.pid})"
        raise OSError(msg)
    return int(opened)


def _resume(handle: int) -> None:
    """Let a process created suspended start running.

    ``ResumeThread`` has nothing to take: CPython closes the child's thread
    handle inside ``Popen._execute_child`` and keeps no thread id, so the
    only way back to a running process is by process handle.
    ``NtResumeProcess`` is not in the SDK headers, but it has been in ntdll
    since NT 4 and is what Sysinternals' ``pssuspend`` uses; the documented
    alternative is to walk every thread in a Toolhelp32 snapshot, which is
    more code to get wrong for the same effect.

    It raises rather than returning a failure: a process left suspended is
    one that will never exit, never write, and never be collected, so a run
    that cannot resume its agent is better off failing here (ADR-0016 covers
    results, not this — nothing has started yet to have a result).
    """
    if sys.platform != "win32":
        raise RuntimeError("resuming a suspended process is Windows-only")
    import ctypes
    from ctypes import wintypes

    ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
    ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
    ntdll.NtResumeProcess.restype = ctypes.c_long
    status = int(ntdll.NtResumeProcess(wintypes.HANDLE(handle)))
    if status < 0:
        msg = (
            "could not resume the process just started "
            f"(NTSTATUS 0x{status & 0xFFFFFFFF:08X})"
        )
        raise OSError(msg)


def _job_for(handle: int) -> int | None:
    """Put the process at ``handle`` in a kill-on-close job; ``None`` if that fails."""
    if sys.platform != "win32":
        raise RuntimeError("job objects exist only on Windows")
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
    try:
        kernel32 = _kernel32()
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            _logger.warning("could not make a job object: %s", ctypes.get_last_error())
            return None
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = job_object_limit_kill_on_job_close
        if not kernel32.SetInformationJobObject(
            job,
            process_extend_limit_information,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            _logger.warning("could not set job limits: %s", ctypes.get_last_error())
            kernel32.CloseHandle(job)
            return None
        if not kernel32.AssignProcessToJobObject(job, wintypes.HANDLE(handle)):
            # Worth saying out loud: without the job this exec's descendants
            # outlive its kill, which is what #105 was.
            _logger.warning(
                "could not put the process in its job object: %s",
                ctypes.get_last_error(),
            )
            kernel32.CloseHandle(job)
            return None
        return int(job)
    except OSError:
        _logger.exception("failed to assign process to a Windows Job Object")
        return None
