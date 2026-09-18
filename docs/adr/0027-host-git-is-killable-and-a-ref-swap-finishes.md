---
status: accepted
---

# Host git is killable, and a ref swap, once started, finishes

The workspace and integrate stages run host git as subprocesses owned by the awaiting task, never in a thread. Cancelling that task kills git and every process it started before the cancellation completes, as ADR-0023 has an exec do. The cancellation might come from a stage's bound firing, from fan-out leaving early, or from whoever called a primitive. `prepare_workspace`, `GitRepo.git`, `GitRepo.open` and `PatchSeries.from_range` are therefore async.

The one git a cancellation waits for is the compare-and-swap `update-ref` that moves a target. Once it starts, it finishes. If the target moved, that landing is what the stage reports: the cancellation is spent (`uncancel` keeps the task's count honest), and `race_timeout` returns work that finished despite its cancellation instead of `TimedOut`. If the swap did not land, the cancellation goes on.

Why: a thread cannot be cancelled. With the landing engine in `asyncio.to_thread`, an integrate bound only stopped waiting. The run reported `TimedOut` and preserved a series that the thread then landed anyway, and the per-repo lock was already free while the thread still wrote. A workspace bound left the clone running and its temp dir behind. Killing is safe everywhere except the swap: every other host write is an unreferenced object or a temp index, and a half-made workspace is deleted. The swap is different in two ways. Git holds the ref's lock while a `reference-transaction` hook runs, so a kill there strands a `.lock` that blocks the next landing. And a kill after the rename would report `TimedOut` for a target that moved.

## Considered options

- Keep the thread; when a bound fires, wait for it and report what happened. Rejected: a bound that waits bounds nothing.
- Keep the thread; stop waiting, then reconcile by re-reading the target and removing a late workspace. Rejected: the result is reported before the truth is known, and the lock is free while the thread still writes.
- Kill the swap as well. Rejected: it can leave a stranded ref lock, or a `TimedOut` for a series that landed.
- Re-raise the cancellation after a swap that landed. Rejected: a cancellation carries no value, so the landing would be reported as a timeout.

## Consequences

If a caller cancels `integrate()` during the milliseconds a swap takes, they get the report, not `CancelledError`: the cancellation arrived after the point of no return. A slow `reference-transaction` hook can stretch a landing past its bound. Four public APIs went from sync to async while v0 was being built. #18 names them but not whether they are sync, and `IntegrationStrategy.integrate` was already async, so a user's strategy just awaits `repo.git`.

Decided in [#57](https://github.com/jeffrichley/waystation/issues/57), after [#27](https://github.com/jeffrichley/waystation/issues/27) made a run's cancellation wait for each stage's own work (ADR-0017).
