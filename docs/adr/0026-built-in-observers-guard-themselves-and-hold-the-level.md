---
status: accepted
---

# A built-in observer guards itself, and a run file holds the level while it is open

Every `on_<hook>` method of a shipped observer is wrapped in `_safely`, which logs at ERROR and returns: a full disk or an unwritable directory costs a log line, never an agent's commits. A user's bundle keeps the opposite contract — it raises, the run fails as `HookRaised`, and the bug is not swallowed (ADR-0016). `RunLogFiles` additionally holds the `waystation` logger at DEBUG for as long as any run file is open, counted so concurrent runs nest, because a record the logger never created cannot be handed to a handler and without `configure_logging` the hierarchy sits at the root's WARNING. The console is insulated from that: `configure_logging` remembers the level it was given and filters its own handler on it, so lowering the logger never changes what a terminal shows, while a logger the script tuned itself still gets through.

Why: the two contracts differ because the blast radius does. A user's hook is their code and a swallowed exception hides their bug, but an observer failing is the thing watching the work breaking the work, which is the failure ADR-0001 exists to prevent. The guard sits on each method rather than inside `HookRegistry.fire` because putting it there would mean the registry asking whether a bundle is ours — a privileged internal path, which the architecture notes say is the signal to stop. Holding the level is the price of the file being complete: the alternative is a run file whose contents depend on whether the script happened to call `configure_logging("DEBUG")`, which is the surprise the observer exists to remove.

## Considered options

- Let a built-in observer raise like a user's. Rejected: observability that can break the work is worse than none.
- Guard inside `HookRegistry.fire` for bundles the library ships. Rejected: the registry would have to recognise its own, and built-ins stop being peers of a user's bundle.
- One `__getattribute__` wrap instead of a decorator per method. Rejected: cheaper to write, far harder to read, and a reader cannot see that a method is guarded.
- Leave the logger alone and write only what the current level creates. Rejected: a run file missing the git argv that explains a failure, depending on an unrelated call, is the bug this feature is meant to fix.
- Raise the console handler's level instead of filtering. Rejected: it breaks per-logger tuning, which ticket 6 settled as the way to turn one stream up.
- End a cancelled run's file through a hook — a cancellation hook, or `run_end` without a result. Rejected: #18 rules out sad-path hooks and a `Cancelled` result, and ADR-0017 fires no hook once a cancellation is held.
- Have the orchestrator tell the built-in observers a run was cancelled. Rejected: the privileged path the guard above already refuses.
- Only `close()` and `with`, as `EventLog`, OpenTelemetry's `SpanProcessor.shutdown` and `logging.Handler.close` end what they hold. Rejected alone: a script that forgets the block leaks for its whole life, and nothing says so.
- Only the task watch. Rejected alone: a run cancelled under `asyncio.timeout` inside a task that goes on keeps its file until that task ends, with no way to end it sooner.
- `weakref.finalize` on the run's context. Rejected: it ends the file whenever the collector gets to it.

## Consequences

`_safely` has to be applied to each new `on_<hook>` a built-in observer grows; a forgotten one fails the run, which is loud rather than silent. The level is given back only if it is still the DEBUG that was taken, so a `configure_logging` call during the window wins. A run cancelled outright never fires `run_end` — it has no result, and no hook fires once a cancellation is held (ADR-0017) — so nothing a hook sees says its file is done. Left there, the handler stays attached and the level stays held for the life of the process, and fan-out's early exit ([#31](https://github.com/jeffrichley/waystation/issues/31)) cancels runs in processes that keep going. So `RunLogFiles` watches the task a run's `run_start` fired in, and when that task ends with the file still open it ends the file `--- no result ---`: the orchestrator's own line just above says where the series went. Fan-out gives each run a task of its own, so its files end the moment the block's exit has stopped them, and Ctrl-C ends the task that awaited a lone run. The watch is also what makes `close()`, and `with RunLogFiles(dir) as files:`, a sweep rather than the only way out: it ends what the task watch could not — a run cancelled under `asyncio.timeout` in a task that goes on — the way `EventLog.close` and `Dashboard`'s exit end what they hold. Whichever of `run_end`, the task ending and `close()` comes first ends the file; the others find nothing. The watch leans on hooks running in the task that performs the run — true of every awaited run, and something a user's bundle may lean on too.

Decided while implementing [ticket 34](https://github.com/jeffrichley/waystation/issues/34), under [ADR-0001](0001-observability-as-hook-bundles.md). How a cancelled run's file ends was settled once fan-out's early exit ([#31](https://github.com/jeffrichley/waystation/issues/31)) made it reachable.
