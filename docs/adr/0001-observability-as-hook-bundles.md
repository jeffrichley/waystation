---
status: stable
type: adr
---

# Observability is opt-in and ships as hook bundles

Rich console output is a hard requirement, yet the library installs no logging handlers. It logs into a `waystation` logger hierarchy behind a `NullHandler`, and a flow script opts in with `configure_logging()` — one idempotent `RichHandler` on **stderr**, because stdout is the agent's Outcome channel. Every built-in observer — per-run log files, the JSONL event log, the live dashboard — is a **hook bundle**: an object with `on_*` methods registered exactly like a user's hooks (`Flow(hooks=[...])`), never a keyword knob such as `log_dir=`.

Why: one Observer mechanism makes built-in and user observability peers and avoids a configuration object, which is the first step onto the framework slope. Auto-installed handlers fight host applications that own their logging, and foreman shows the failure mode of partial wiring: handlers on a few named loggers, every other module's output silently dropped to stdlib's last-resort handler.

## Two channels, and what each carries

Hooks are not the only way a run reports itself, and pretending otherwise is what made the second channel invisible. **Hooks carry the stage boundaries a run passes through, each with the values that boundary produced.** They are typed, they are the ones a flow script acts on, and #18 rules out sad-path ones: there is no cancellation hook, and no `Cancelled` result for `run_end` to be handed (ADR-0017).

**The `waystation.run` log records carry every lifecycle event, the two hooks don't included** — the agent starting, which opens between `sandbox_ready` and the first output line, and a run being cancelled. Each record names its event in an `event` extra, so a bundle reads the channel by attribute rather than by parsing prose. The names, levels and extras are `docs/log-records.md`, and a test holds them.

That the second channel exists does not make built-ins privileged, which is the thing this ADR is for. `RunLog` is handed those two events by the orchestrator directly, and a user's bundle is not — but what `RunLog` does with them is write an ordinary record on a documented, public channel, which any bundle can attach a handler to and read. The line an observer would need and could not get is what a privileged path means here; a line every observer can read is not one. Before #78 that was true only by accident: the records were there, and nothing said they were an interface, so in practice the built-ins had a channel to themselves.

An adapter of the user's own — a `SandboxBackend`, an `AgentProvider` — joins the same channel with `run_logger`, so its lines land in a run's files beside the shipped ones. Those two protocols take no context and no logger, deliberately (ADR-0010, ADR-0018), so a factory is the seam rather than a parameter on every implementation.

## Considered options

- Install a `RichHandler` on import, or lazily on the first run.
- Feature knobs on `Flow` (`log_dir=`, `events=`, `dashboard=True`).
- Ship the dashboard and event log as `examples/` only, built on public hooks. Rejected by the user: both are core features.
- Give the two hook-less events hooks of their own, so one channel carries everything. Rejected: #18 rules out sad-path hooks and ADR-0017 fires none once a cancellation is held, so the cancellation hook could not fire where it is wanted.
- Hand an adapter its logger as a parameter — `start(ws, *, env, log)`, `command(prompt, schema, log)`. Rejected: it changes two protocols every backend and provider implements, to serve the ones that log, and a pure provider (ADR-0018) does not want a logger threaded through its signature.
- Let `run_logger` take any name the caller likes. Rejected: a run's observers attach to the package logger and `logging` routes by dotted name alone, so a logger outside the hierarchy is never reached however it is tagged. Celery's `get_task_logger` and Prefect's `get_run_logger` parent a caller's logger for the same reason. Reaching a free-named logger would mean a registry of joined loggers, or a root handler that ADR-0026's propagation cut would then starve of the DEBUG records a run file exists to keep.

Decided in [wayfinder ticket 6](https://github.com/jeffrichley/waystation/issues/6). Amended in [#78](https://github.com/jeffrichley/waystation/issues/78): the log records were already the channel for the two events hooks don't carry, and saying so — with an `event` on each record and `run_logger` for an adapter's own lines — is what makes them a channel a user's bundle can read rather than one the built-ins had to themselves.
