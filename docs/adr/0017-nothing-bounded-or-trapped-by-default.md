---
status: accepted
---

# Nothing is bounded or trapped by default: timeouts and signal handling are opt-in

Every stage can be bounded orchestrator-side through one `Timeouts` value object — `workspace`, `sandbox`, `agent_silence` (reset on every output line), `agent_wall`, `collect`, `integrate`, `teardown`, `completion_grace` — set as a `Flow` default and replaced per run, but every failure bound defaults to `None`, unbounded. The sole default number is `completion_grace=30`: the silence, after the Outcome line lands, before the run proceeds and `rm -f` kills whatever still holds stdout open (an MCP server, `gh`); `None` there would mean "wait for EOF", which is the hanging-child bug itself. A run that exceeds a bound fails with `TimedOut`; a run whose Outcome line was seen but whose EOF never came succeeds with `hanging=True`. Hooks are never bounded. The library installs no signal handlers: `asyncio.run` already maps SIGINT to task cancellation, under which the salvage `WIP` + `format-patch` runs shielded (unbounded — git terminates on its own, and a second signal is the escape hatch), the integrate stage is atomic (it has not started, or it completes or aborts cleanly before the cancellation surfaces), and `finally` tears the sandbox down. SIGTERM is covered by an opt-in `handle_signals()` — SIGINT + SIGTERM on POSIX, SIGINT + SIGBREAK on Windows — that cancels the running task once and lets a second signal fall through to default behaviour.

Why: the library never decides policy, and a 600 s idle default is policy — sandcastle shipped internal constants and walked them back one knob at a time. Process-global signal handlers fight host applications (a typer script, a web service, pytest, a notebook) exactly the way auto-installed logging handlers do, so ADR-0001's shape applies unchanged: install nothing, ship one explicit helper the examples call beside `configure_logging()`.

## Considered options

- Sandcastle's defaults (600 s idle, 120 s start, 60 s completion grace, …) as internal constants. Rejected: policy in the library.
- "Very high" defaults. Rejected: still magic numbers, only bigger.
- A bounded cancel-salvage window. Rejected: a number defending against a wedged docker daemon the user can already escape with a second Ctrl-C.
- Per-hook timeouts. Rejected for v0: bounding user code is the framework slope; a hung hook is visible as a stalled stage in the dashboard.
- Auto-installing handlers at `flow.run()` or fan-out entry and restoring them on exit. Rejected: still process-global while active, and `loop.add_signal_handler` does not exist on Windows.
- A per-run abort handle (`ctx.cancel()` from a hook) with a `Cancelled` failure kind. Deferred: silence and wall bounds cover runaway agents; add it when a real flow needs one.

## Consequences

A stuck agent — no output, waiting on a prompt — hangs its run until cancelled; the dashboard shows elapsed climbing and nothing else fires. The examples set explicit bounds from rung 1 and call `handle_signals()`, so the pattern is taught rather than imposed. SIGTERM without the helper is a hard kill: sandboxes are orphaned for the reaper and uncommitted agent work is lost.

Decided in [wayfinder ticket 14](https://github.com/jeffrichley/waystation/issues/14), informed by the [sandcastle failure-semantics research](https://github.com/jeffrichley/waystation/issues/13).
