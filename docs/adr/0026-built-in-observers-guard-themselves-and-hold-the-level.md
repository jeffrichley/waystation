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

## Consequences

`_safely` has to be applied to each new `on_<hook>` a built-in observer grows; a forgotten one fails the run, which is loud rather than silent. The level is given back only if it is still the DEBUG that was taken, so a `configure_logging` call during the window wins. A run cancelled outright never fires `run_end`, so the handler stays attached and the level stays held for the life of the process — cancellation is issue 27's subject, and the console filter is what keeps that leak from being visible.

Decided while implementing [ticket 34](https://github.com/jeffrichley/waystation/issues/34), under [ADR-0001](0001-observability-as-hook-bundles.md).
