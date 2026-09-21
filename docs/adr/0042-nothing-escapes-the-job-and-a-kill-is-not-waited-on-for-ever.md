---
status: draft
type: adr
---

# Nothing escapes the job, and a kill is not waited on for ever

On Windows a child is created suspended, put in its Job Object, and only then resumed — so it cannot start anything before the thing that kills it exists. And the wait after a kill is bounded: a tree that outlives its kill is logged and abandoned, rather than parking the cancellation for ever.

This is ADR-0023's mechanism, corrected. The decision there — kill a tree, never a process — stands; what it did not say was *when* the tree has to exist.

## The window was the bug

`AssignProcessToJobObject` adds **one process**. Not the descendants it already has — the documentation is clear, and the behaviour is the point of Raymond Chen's standing advice that a process must be in its job before it runs.

Taking charge happened after `asyncio.create_subprocess_exec` returned, and that is not instant: the spawn was measured at **150 ms to 3 s** on a loaded host. Git Bash forks within a few milliseconds of starting, and `while true; do sleep 0.05; done` forks twenty times a second. So under load the job routinely held one process while the shell already had children outside it.

Those children survived `TerminateJobObject`, which did exactly what it was asked. They also held the stdout they inherited — and **`Process.wait()` on Windows returns once every pipe has closed, not once the process has exited**. `BaseSubprocessTransport._try_finish` wakes the exit waiters only `if all(p.disconnected for p in self._pipes.values())` (cpython [gh-119710](https://github.com/python/cpython/issues/119710); reproduced here on 3.12.10, 3.13.9 and 3.14.6 alike, so no Python upgrade avoids it).

One escapee therefore wedged `await process.wait()` in the cancellation handler, for ever. Everything waiting on that run waited with it: the stage, the run, the fan-out's `__aexit__`. On CI the test hung past its timeout and pytest-timeout ended the worker with `os._exit`, which reads in the log as `worker 'gwN' crashed` and nothing else (#105).

Measured, spawning suspended against not: **0 hangs in 48** against **14 in 16**, with the job holding the descendants rather than just the shell.

## Two changes, because one is not enough

**Create suspended, assign, resume** removes the cause. Nothing has run, so there is nothing to leave behind.

**Bound the wait** removes the consequence, and it earns its place separately. A descendant can escape for reasons this ADR has not thought of — a breakaway flag, a handle inherited some other way — and the failure mode is the worst one available: not a crash, not an error, but a program that can no longer be cancelled. asyncio's cancellation is **edge-triggered**, so a task that blocks again inside its own cancellation handler is never cancelled a second time. Trio's documentation names this exactly: "if we were using asyncio … since our timeout already fired, it wouldn't fire again, and at this point our application would lock up forever."

That is not the caller choosing to wait. It is the caller losing the ability to choose anything again, Ctrl-C included.

**`taskkill` is gone.** It was there to reach children started before the job existed — the very window this closes. It never did: across 129 kills in an instrumented run it returned "process not found" **every time**, because the job kill had already worked, while costing about half a second of *blocked event loop* per exec. That is the same defect ADR-0037 moved workspace removal off the loop to avoid. It also cannot do the job in principle — it walks live parent-child links in a snapshot, so it misses precisely the orphan whose parent has already exited.

## The number, and ADR-0017

Five seconds is a bound in a codebase that says a bound nobody asked for does not get to exist. It is asked for here, and it is the same shape as ADR-0037's removal retry: not a budget for a killed tree to die in — that takes under a millisecond — but the point at which we stop believing it will.

It is not a knob. A caller who wants to wait longer for a process that has already been killed is asking for the hang, and a caller who wants to give up sooner can cancel the run. What is left is logged at ERROR, naming the pid and saying what it means for the workspace about to be removed.

## Considered options

**Leave the wait unbounded and fix only the window.** The stdlib's own precedent is on this side: `subprocess.run`'s timeout path calls `process.kill()` and then waits unbounded, and Trio deliberately promises `run_process` always waits for the child. Rejected because those are both *level-triggered or synchronous* settings where a second cancellation still arrives. In an asyncio cancellation handler an unbounded wait is unreachable by any further cancellation, and the observed failure was not theoretical.

**`PROC_THREAD_ATTRIBUTE_JOB_LIST`** puts the process in the job at creation, race-free, with no suspend. Unreachable from Python: CPython's `subprocess` exposes only `handle_list` in `STARTUPINFO.lpAttributeList`, so using it means reimplementing `CreateProcessW` and asyncio's pipe plumbing.

**Enumerate and kill descendants (psutil-style).** A snapshot walk has the same blind spot as `taskkill` — an orphan whose parent exited is unreachable — and it would add a dependency to do worse than the job object already does.

**Resume with `ResumeThread`.** There is no thread to resume: CPython closes the child's thread handle inside `Popen._execute_child` and keeps no thread id. `NtResumeProcess` takes the process handle instead. It is not in the SDK headers, but it has been in ntdll since NT 4 and is what Sysinternals' `pssuspend` uses; the documented alternative is walking every thread in a Toolhelp32 snapshot, which is more code to get wrong for the same effect.

## The cost, stated

Creating suspended trades the escape race for a smaller one: a crash between spawning and resuming leaves a process suspended with nobody to resume it. That is strictly the better failure — an inert process that holds no pipe and wedges nothing, against a live one that wedges every await on the run — and resuming is one syscall away from the spawn, inside a `finally`. A resume that fails raises rather than returning a tree, because a process nothing will ever resume is one nothing will ever collect.

## Also corrected here

The ctypes prototypes are declared. Without `restype`, ctypes returns a C `int`, truncating a 64-bit `HANDLE` and making one with the top bit set negative; without `argtypes` every argument marshals as a 32-bit int. Handle values happened to be small enough that this worked — which is what made it worth fixing, since the failure would be silent and would arrive on someone else's machine. A mismatch raises `ctypes.ArgumentError`, which is not an `OSError` and would have walked straight out through the `suppress(OSError)` around the kill.

The process handle is the one asyncio already holds, read off the transport, rather than a fresh `OpenProcess(pid)`. A pid identifies a process only while something holds a handle to it; reopening one asyncio is already holding buys nothing but the chance of opening somebody else's. It is the hazard `PosixProcesses.release` already declines to take, now declined on both sides.
