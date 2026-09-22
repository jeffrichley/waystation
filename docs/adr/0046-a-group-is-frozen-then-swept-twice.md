---
type: adr
title: 0046 A Group Is Frozen Then Swept Twice
status: stable
---

# A process group is frozen before it is swept, and swept again before its leader is reaped

`PosixProcesses` kills a tree by sending `SIGSTOP` to the exec's process group, then `SIGKILL` to the same group, then killing the leader. The runner sweeps the group once more after a 5 ms settle, and that second sweep happens **before** `process.wait()` reaps the leader. Never after.

This is ADR-0023's mechanism on POSIX, corrected, as ADR-0042 corrected it on Windows. The decision there — kill a tree, never a process — stands. What neither said was that **one sweep of a group is not atomic against a fork**.

## The window was the bug

`kill(-pgid, SIGKILL)` walks the process table and signals the members it finds. A child that its parent is forking as the walk goes past is created with the group inherited and the signal already spent. Its parent then dies, and the child is reparented to init, still in a group nobody will sweep again.

So the promise story 60 makes — nothing the agent started is still writing when collect reads the workspace — did not hold. `tests/test_hooks.py` showed it as a test that took 5.3 s rather than 1.9 s: the survivor held the exec's stdout, `Process.wait()` waited for it, and ADR-0042's grace ended the wait and logged it. The run then collected the workspace with a live process in it.

## How it was measured

A harness cancels an exec on its first output line, then reads `ps` for anything left in that exec's group:

| | survivors |
| --- | --- |
| before, 100 cancellations | **17** |
| before, 50 | 9 |
| after (freeze + settled second sweep), 100 | 0 |
| after, 50 | 0 |

`os.killpg` returned success on every failing run, and one more `killpg` on the same group afterwards cleared the orphan every time — so the signal was never the problem, and the group was always the right one to sweep.

Two probes made the fault vanish by perturbing the schedule: one that snapshotted `ps` before the kill, and one that wrapped the wait in a task. That is why the test below injects the miss instead of racing for it.

## Why freeze, when the second sweep already measured clean

A second sweep alone also measured 0 in 100. It is only ever *likely* to be enough, though: an escapee that survived the first sweep is running, and a running process can fork again, so any fixed number of sweeps can be outrun in principle. `SIGSTOP` removes that. A stopped member cannot start a new fork, so what can still land is only what was already in flight, which the settled sweep gets. `SIGSTOP`, like `SIGKILL`, is the kernel's to deliver and cannot be caught, and a stopped process takes a `SIGKILL` as it is, so no `SIGCONT` is needed (jmmv, ["How to kill a tree of processes"](https://jmmv.dev/2008/01/how-to-kill-tree-of-processes.html)).

## Why the second sweep must come before the reap

A group id is the leader's pid, and it is the kernel's to hand out again once the group is empty and the leader reaped. Sweeping after that can signal a group some unrelated process has been given since — the defect open against Bazel's `process-wrapper` as [bazelbuild/bazel#11910](https://github.com/bazelbuild/bazel/issues/11910). While the leader is unreaped the id is still this tree's, and `process.wait()` is what reaps it, so the sweep goes immediately before that wait. `release()`'s existing note — that a group is only killed while its exec is still awaited — is the same rule.

## The numbers, and ADR-0017

5 ms is a bound in a codebase where a bound nobody asked for does not get to exist. It is asked for here: it is how long a fork already in flight needs to land, not a budget for anything to finish in. It is not a knob, because a caller cannot have an opinion about it, and the sweep runs either way.

## Considered options

- **Sweep repeatedly until the group is empty.** Rejected: there is no portable way to ask whether a group still has live members. `killpg(pgid, 0)` succeeds while the unreaped zombie leader is in it, so the loop's own condition is never false.
- **Sweep after the leader is reaped, when the orphan is easy to find.** Rejected for the pid-reuse defect above, even though it is the shape the diagnosis used to prove the orphan was reachable.
- **`PID` namespaces or `cgroup.kill`.** The kernel does this atomically and is protected against racing forks by design, which is what `cgroup.kill` exists for ([LWN](https://lwn.net/Articles/855924/)). Rejected for v0: both are Linux-only, `cgroup.kill` needs cgroup v2 and delegation, and waystation's POSIX path also carries macOS, which has neither. A Linux-only backend may use it later; the strategy seam is where it would go.
- **`SIGSTOP` the tree and walk it with `ps`, killing leaves first.** Rejected: it shells out per kill, it is the recipe jmmv's own follow-up abandoned, and the group already names every descendant.
- **Leave it, since ADR-0042's grace stops the hang.** Rejected: the grace bounds the damage, it does not stop a live process writing into a workspace being collected, and it is what hid this for two sightings.

## Consequences

Every cancelled exec now pays 5 ms and two extra signals. On the cancellation path, where a process is already being killed, that is nothing.

`ProcessTree.kill()` must be safe to call more than once. It already is on both hosts: `TerminateJobObject` on a terminated job and `killpg` on a gone group are both no-ops, and both implementations suppress `OSError`. The protocol's docstring now says so, since a backend author writing a third strategy has to know.

`tests/test_processes.py::test_a_child_the_first_sweep_missed_is_dead_before_the_cancel_ends` holds it with `_MissesOnce`, a strategy whose first sweep kills only the leader, leaving what it started behind exactly as a raced fork does. The window is a number, as `_SlowToAdopt` (ADR-0042) and `_SlowToDie` (#169) made theirs. Racing the kernel for it would need about a hundred cancellations per handful of survivors, and two of the probes that did race it changed the timing enough to hide the fault.

Decided in [#110](https://github.com/jeffrichley/waystation/issues/110).
