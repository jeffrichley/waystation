---
status: stable
type: adr
---

# Hooks are an Observer with snapshot binding

Hooks register at two levels — `Flow(hooks=...)` or `@flow.on_*`, and per run via the builder's `.on_*()` — and both feed one registry. `flow.run()` snapshots the flow's hooks at call time; chained hooks append; flow-installed hooks fire first; all fire, none shadow. Why: value semantics remove action at a distance — a hook added to the flow after a run was built does not reach that run — which resolves the "what am I attaching to" ambiguity a live reference would create.

## Considered options

- A live reference to the flow's hook list.
- Per-run hooks shadowing flow hooks of the same name.

Decided in [wayfinder ticket 4](https://github.com/jeffrichley/waystation/issues/4).
