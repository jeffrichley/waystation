---
status: accepted
---

# Observability is opt-in and ships as hook bundles

Rich console output is a hard requirement, yet the library installs no logging handlers. It logs into a `waystation` logger hierarchy behind a `NullHandler`, and a flow script opts in with `configure_logging()` — one idempotent `RichHandler` on **stderr**, because stdout is the agent's Outcome channel. Every built-in observer — per-run log files, the JSONL event log, the live dashboard — is a **hook bundle**: an object with `on_*` methods registered exactly like a user's hooks (`Flow(hooks=[...])`), never a keyword knob such as `log_dir=`.

Why: one Observer mechanism makes built-in and user observability peers and avoids a configuration object, which is the first step onto the framework slope. Auto-installed handlers fight host applications that own their logging, and foreman shows the failure mode of partial wiring: handlers on a few named loggers, every other module's output silently dropped to stdlib's last-resort handler.

## Considered options

- Install a `RichHandler` on import, or lazily on the first run.
- Feature knobs on `Flow` (`log_dir=`, `events=`, `dashboard=True`).
- Ship the dashboard and event log as `examples/` only, built on public hooks. Rejected by the user: both are core features.

Decided in [wayfinder ticket 6](https://github.com/jeffrichley/waystation/issues/6).
