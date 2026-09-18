---
status: accepted
---

# Host git is killable, and a ref swap, once started, finishes

The workspace and integrate stages run host git as subprocesses owned by the task that awaits them, never in a thread. So `prepare_workspace`, `GitRepo.git`, `GitRepo.open` and `PatchSeries.from_range` are async. Cancelling that task kills git and every process it started before the cancellation completes, just as ADR-0023 has an exec do.

Inside a run, what cancels a stage's git is its bound. The run's own cancellation, a signal or fan-out leaving early, waits for the stage instead (ADR-0017). Only a direct caller of a primitive cancels git outright.

The one git nothing kills is `update-ref`: `GitRepo.git` runs it as a commit point. Once started, it finishes.
- A cancellation that arrives meanwhile waits for it, then is raised. It is never spent.
- A stage's bound that runs out meanwhile waits for it before firing. If the stage's work finished in that time, the finished work is the result, not `TimedOut`.

A user's own strategy moves its target through the same `GitRepo.git`, so it gets the same guarantee as the shipped `Integration`. There is no privileged path.

Why: a thread cannot be cancelled. With the landing engine in `asyncio.to_thread`, an integrate bound only stopped waiting. The run reported `TimedOut` and preserved a series the thread then landed, and the per-repo lock was free while it still wrote. A workspace bound left the clone running and its temp dir behind.

Killing is safe everywhere except `update-ref`. Every other host write is an unreferenced object or a temp index, and a half-made workspace is deleted. `update-ref` is different in two ways. Git holds the ref's lock while a `reference-transaction` hook runs, so a kill there strands a `.lock` that blocks the next landing. And a kill after the rename would report `TimedOut` for a target that moved.

## Considered options

- **Keep the thread, and wait for it when a bound fires.** Rejected: a bound that waits bounds nothing.
- **Keep the thread, stop waiting, and reconcile afterwards.** Rejected: the result is reported before the truth is known, and the lock is free while git still writes.
- **Kill `update-ref` too.** Rejected: a stranded ref lock, or a `TimedOut` for a series that landed.
- **Let the bound fire into the swap, then spend the cancellation on a swap that landed** (`uncancel`, and return the report). Tried and rejected in review. Spending a cancellation also spends a direct caller's Ctrl-C and confuses `asyncio.timeout` and `TaskGroup` bookkeeping. And the guard, being private to the shipped strategy, left a user's own strategy unprotected.

## Consequences

- A slow `reference-transaction` hook stretches a landing past its bound. The bound waits for the swap it cannot safely stop.
- A caller who cancels `integrate()` mid-swap gets `CancelledError` after the swap. The target may have moved, so they re-read it.
- A strategy that runs more git after its `update-ref` can still time out after moving its target. The shipped one returns straight after its swap.
- On POSIX each git runs in its own session so that its group can be killed. So a hard second signal (ADR-0017) ends Python and leaves a running clone or landing to finish on its own.
- On Windows, a job is assigned just after spawn. A descendant started in that instant is reached by `taskkill /T` only while its parent lives. The same gap is ADR-0023's for execs.
- Four public APIs went from sync to async while v0 was being built. #18 names them without saying which, and `IntegrationStrategy.integrate` was already async.

Decided in [#57](https://github.com/jeffrichley/waystation/issues/57), after [#27](https://github.com/jeffrichley/waystation/issues/27) made a run's cancellation wait for each stage's own work (ADR-0017).
