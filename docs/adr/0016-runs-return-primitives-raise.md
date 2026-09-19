---
status: accepted
---

# Runs return typed failures; primitives raise

A run that has begun always returns `RunSucceeded | RunConflicted | RunFailed`, whether awaited directly or through fan-out (ADR-0007). Primitives raise instead: a `WaystationError` family of exactly two members, `PreflightError` and `StageError(stage, failure)`, where `failure` is the same tagged union `RunFailed` carries — `TimedOut`, `AgentExited`, `OutcomeMissing`, `OutcomeInvalid`, `HookRaised`, `CommandFailed`, `Refused`, `Errored`. The run is the adapter between the two worlds: it catches `StageError` and *every other* `Exception` into `RunFailed(stage, failure)` (`Errored` keeps the exception for `raise result.failure.exception`); `BaseException` is never caught into a result, so cancellation propagates — held, at most, until the stage it lands in has finished its own work (ADR-0017). Fan-out raises only `PreflightError`, and only before its first result — an argument no batch could run under, a `max_concurrency` below 1, is refused at the call, as `flow.run()` refuses an Outcome that is not an object; a lone awaited run preflights itself the same way and raises it before the run begins, with no hook fired and no workspace made — a run that fails preflight never started, so there is no result to return ([#30](https://github.com/jeffrichley/waystation/issues/30)). `stage` draws from the five-stage vocabulary; a hook that raises is attributed to the stage whose boundary fired, with `HookRaised` naming the hook — `hook` is a cause, not a stage. A non-zero agent exit is a failure even when a valid Outcome line is present (the parsed Outcome rides along); an Outcome is always required; missing and invalid are distinct kinds. On any failure past agent start, collect still runs best-effort and a non-empty series lands on the preservation branch, never integrated — widening ADR-0005. Every result carries the run id, name, elapsed per stage, and an `AgentExit(exit_code, elapsed, hanging)` when the agent ran; `RunFailed` and `RunConflicted` both carry the preserved series.

Why: the fan-out promise — every run reports — is the product, and it only holds if nothing escapes; a result with the exception attached costs the flow script one `raise`, while an escaped provider bug costs it the whole batch. One `Failure` union on both sides keeps primitive users and run users in one vocabulary without a parallel exception hierarchy.

## Considered options

- One exception class per stage timeout and error, sandcastle's shape (24 tagged classes). Rejected: fan-out yields, so the taxonomy folds into one result with typed fields per kind on the failure union.
- Catching only `WaystationError` and letting everything else propagate. Rejected: breaks ADR-0007 on the first bug in a provider or backend.
- `stage="hook"` as a sixth stage. Rejected: the dashboard column, the hook boundaries and `RunFailed.stage` share one five-word vocabulary.
- An optional Outcome when the run supplies no schema. Rejected: the Outcome line is the completion contract (ADR-0009).
- Losing the series when a run fails. Rejected: never lose agent work — sandcastle's dirty-worktree preservation, transferred to the ephemeral-sandbox model.
- Parsing git's stderr to upgrade a `CommandFailed` into a `Refused`. Rejected: `Refused` is reserved for preconditions waystation checks itself (dirty working tree on a head target, missing `extra_ref`, image gone since preflight; [wayfinder ticket 7](https://github.com/jeffrichley/waystation/issues/7) adds a target branch checked out in another worktree, a target moved outside the process, a non-linear series and a host with no git identity).

## Consequences

Teardown failure never changes the result kind: it is logged at ERROR and the labelled sandbox is the reaper's problem (ADR-0014). The only retry anywhere is exit 126/137 on idempotent setup execs, twice, 250 ms apart, with no knob; run-level retry belongs to the flow script, fed by `elapsed`, `exit_code` and `stage` on `RunFailed`.

Decided in [wayfinder ticket 14](https://github.com/jeffrichley/waystation/issues/14), informed by the [sandcastle failure-semantics research](https://github.com/jeffrichley/waystation/issues/13).
