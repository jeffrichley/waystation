---
status: accepted
---

# The sandbox backend protocol is behavioral; argv purity is an internal idiom

The protocol is an async lifecycle — `preflight()`, `start(spec)` returning a `Sandbox` context manager, `sandbox.exec(...)` with a buffered result plus live line callbacks. Docker and bwrap implement it internally through pure frozen-dataclass argv builders (`plan_create` / `plan_exec` / `plan_destroy` feeding one dumb runner) so mount and env plans unit-test without Docker (amended by ADR-0035: the runner is public, because a backend that had to rewrite it would be rewriting most of what `exec` promises, and a conformance suite states that contract; argv purity stays internal, as each backend's own). Why: purity is a testing idiom, not a contract — `NoSandbox` has no argv at all, so a protocol shaped around argv would exclude the backend that dev and tests rely on.

Decided in [wayfinder ticket 5](https://github.com/jeffrichley/waystation/issues/5).
