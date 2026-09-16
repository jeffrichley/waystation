---
status: accepted
---

# A run is an immutable value, and nothing integrates unless asked

`flow.run(prompt, outcome=...)` returns a frozen `RunSpec[O]` — a value describing a run, not a run. Every builder method returns a new `RunSpec`, so a half-built one forks safely, and every `await` of a `RunSpec` starts a fresh run with a new run id — retry in a flow script is awaiting the same value again. The base ref resolves to a sha when the workspace stage starts, not when the value is built, so a retry, or a run queued behind a concurrency cap, starts from the ref as it is then; every result records `base_sha`. The library's integration default is none: a `Flow(integration=...)` default or a per-run `.integrate(...)` opts in, `.integrate(None)` opts a run back out, and a non-empty series from a run that integrates nowhere lands on its preservation branch, where the integrate primitive can pick it up later (ADR-0015).

Why: value semantics are ADR-0008's rule for hooks carried to the whole run, and they remove aliasing between runs forked from one builder — the shape of SQLAlchemy 2.0's generative `select()` and Django's QuerySet. A default of `HEAD` + `apply` would let an outcome-only research run move the user's checkout, a policy the library must not own; and a series the flow never integrates is still agent work that must not vanish with the sandbox.

## Considered options

- A mutating builder that returns `self`.
- A single-use awaitable, coroutine-style.
- Resolving the base when `flow.run()` is called.
- `HEAD` + `apply` as the default integration.
- Keeping a non-integrated series only in memory on the result.

Decided in [wayfinder ticket 7](https://github.com/jeffrichley/waystation/issues/7).
