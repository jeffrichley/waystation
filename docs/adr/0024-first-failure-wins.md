---
status: accepted
---

# The first failure wins; later failures are logged

A run reports the first failure it meets. Anything that fails after that — a hook raising at `agent_end` or `run_end` once the run has already failed, collect running best-effort after an agent failure, teardown — is logged at ERROR on the `waystation` logger and never replaces the failure the result carries. A raising hook on an otherwise healthy run still fails it as `HookRaised`; a failure with nothing before it is always the one reported. This generalises ADR-0016's teardown rule ("teardown failure never changes the result kind") to every secondary failure.

Why: the first failure is the cause and everything after it is usually a symptom — a killed agent leaves `index.lock` behind, so collect's `git add` fails; a dashboard hook trips over the already-failed state. Reporting the symptom hides the cause and, for collect, costs the preserved series. The precedents agree: Guava's `Closer` throws the try block's throwable and only suppresses close failures, and Go's defer-close idiom assigns the close error only when `err == nil`.

## Considered options

- Latest failure wins, so a raising hook always surfaces as `HookRaised`. Rejected: a buggy observer would overwrite `AgentExited` or `TimedOut`, the facts a flow script branches on.
- Carrying every failure on the result (a list, or Go's `errors.Join`). Rejected: `RunFailed.failure` is one tagged value a flow script `match`es on, and a second failure is almost always a consequence of the first.

Decided while implementing [issue #28](https://github.com/jeffrichley/waystation/issues/28), from its code review.
