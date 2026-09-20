---
status: accepted
---

# A stage runner carries a run's guarantees to a hand-composed loop

The bound a stage runs under, the cancellation held until that stage's own work ends, and the elapsed time it took move out of the run orchestrator into a **stage runner**: `stages(bounds: Timeouts = Timeouts())`, a run-scoped value used as `async with`, returning a private class the way `fan_out()` does. A composer writes `await run.stage(stage, work, *, bound=stage)` — `bound` naming the `Timeouts` field, `bound=None` for the agent stage, whose timers `run_agent` owns, and `bound="teardown"` for leaving a sandbox — and `async with run.entering(stage, cm) as box` to bound entering a context manager by its stage and leaving it by `teardown`, which keeps teardown before integrate where it belongs. `run.elapsed` reads back seconds per stage, measured on the clock seam. A bound that fires raises `StageError(stage, TimedOut(...))`, as a primitive does (ADR-0016). A cancellation that arrives mid-stage surfaces at the next `stage()` call, so the next stage never begins, and again at the block's exit; it outranks an exception already unwinding, the rule fan-out's exit already follows. One stage runner serves one run.

What stays with whoever composes, `RunSpec` included: which stages to run, first-failure-wins bookkeeping (ADR-0024), best-effort collect after a failure past agent start, whether a series is preserved, and the rule that a teardown failure never masks a result (ADR-0016). `preflight(specs)` and `preserve_series` become public beside `stages`, and a spec whose batch was already checked runs through `await spec.perform(preflighted=True)` — `await spec` delegates to it — so fan-out stops reaching into a private method. `run_to_end` and `committed` stay internal: the stage runner is the public face of the shield.

Why: #18 story 105 offers the five stages as public primitives "so that I can compose a custom loop", but every guarantee that makes a run safe lived inside `RunSpec`. A hand-composed loop cancelled by Ctrl-C tore its sandbox down and threw away the agent's uncommitted work; an `asyncio.timeout` around `integrate` could turn a landing that had finished into a `TimeoutError`; and `Timeouts` could not be applied at all — [#40](https://github.com/jeffrichley/waystation/issues/40)'s rung 3 is required to compose by hand *and* to set explicit bounds, which today cannot both be true. Holding a cancellation is state that spans stages, so free functions could not carry it; a context manager is what lets the last held cancellation surface on the way out instead of escaping into the composer's own `finally`. The composer still drives the order, so this is not a template to fill in — the architecture notes rule out Template Method, and this keeps inversion where it was.

## Considered options

- **Publish `run_to_end`, `committed` and a `bounded()` helper, and let the composer hold the state.** Rejected: a cancellation held across stages has nowhere to live, which is the guarantee most worth carrying.
- **A runner object with no context manager.** Rejected: a held cancellation would surface only at the next `stage()` call, so the last one escapes uncaught.
- **The stage runner closes what it entered at block exit.** Rejected: the sandbox must be gone before integrate, and the block ends after it. `entering()` nests instead.
- **A public function that runs all five stages given hooks.** Rejected: that is `RunSpec`, and a second one is Template Method.
- **Leaving fan-out's private `_execute(preflighted=True)` in place.** Rejected: a user's own scheduler cannot take that path, and #18 promises preflight-once as a property of a batch, not of fan-out.
- **Logging stage starts and ends from the stage runner.** Rejected: logging is run-level and tied to a run id the stage runner has no reason to know; a composer wanting tagged lines wants [#78](https://github.com/jeffrichley/waystation/issues/78)'s record channel.

## Consequences

`RunSpec` becomes the stage runner's first user rather than the only holder of the rules, so every cancellation, timeout and stage-bounds test in the suite exercises the public piece. `Timeouts` gains a second reader, and the mismatch it already had — teardown is bounded by `Timeouts.teardown` but attributed to the sandbox stage — is now visible at a call site instead of buried in a private helper. Elapsed moves onto the clock seam, so `ManualClock` drives it; [#83](https://github.com/jeffrichley/waystation/issues/83) hands that item over rather than editing the same lines. `CONTEXT.md` gains **stage runner**, and the import surface gains `stages`, `preflight` and `preserve_series`, each an addition #18's list has to name.

Decided while grilling [#70](https://github.com/jeffrichley/waystation/issues/70), out of the architecture review in [#69](https://github.com/jeffrichley/waystation/issues/69).
