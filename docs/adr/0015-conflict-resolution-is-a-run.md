---
status: accepted
---

# Conflict resolution is a run, not a seam

When integration conflicts, waystation resolves nothing: it aborts, preserves the series on a preservation branch (ADR-0005) and returns `RunConflicted(outcome, report)`, whose report names the preservation branch, the base and target shas, the mechanism, the conflicting paths and the failed patch. Resolution is an ordinary resolver run the flow script launches: workspace at the target, the preservation branch made visible inside the sandbox by an `extra_refs` list on workspace prep (same-name local refs, default empty), and the agent instructed to replay the series by cherry-pick rather than merge so the product stays a linear series (ADR-0006). The integrate primitive accepts a series built from any `base..ref` range, so a hand-resolved preservation branch or a later retry lands through the same path. There is no conflict hook: `run_end` carries the typed result and observers match on it. Why: the integrate primitive holds the per-repo lock, so an agentic handler inside integration would either stall every other run's integration for minutes or release the lock and be a run anyway, and it would nest sandbox and agent inside the integrate stage, breaking stage attribution.

## Considered options

- A conflict-handler slot on the integration strategy. Rejected for the lock and stage-nesting reasons above; mechanical handlers (`rerere`, retry-as-merge) already have a home as a custom integration strategy.
- A core `resolve()` helper with a canned prompt. Rejected: prompts are user-owned (ADR-0004); the resolver lives in `examples/`.
- Falling back from `apply` to `merge` inside the shipped strategy. Rejected: fail fast, and a silent mechanism switch writes a target history nobody asked for.
- `conflicted` and `failed` hooks. Rejected: hooks mark boundaries and the outcome rides the payload, the shape of Go's `httptrace`, OpenTelemetry span processors and Kubernetes container hooks. Every existing hook is a happy-path boundary; the first sad-path hook drags `sandbox_failed`, `workspace_failed` and `agent_failed` behind it. Re-open only if a real observer needs the conflict before `run_end`.

Decided in [wayfinder ticket 11](https://github.com/jeffrichley/waystation/issues/11).
