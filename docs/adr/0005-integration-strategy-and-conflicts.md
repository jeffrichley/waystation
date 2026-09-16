---
status: accepted
---

# Integration is a pluggable strategy; conflicts abort and preserve

How a run's patch series reaches the host repo is an integration strategy (GoF Strategy). Waystation ships one, parameterized by **target** (head, or a named branch created at the base ref if missing) × **mechanism** (apply = linear `git am --3way`; merge = a merge commit via a temp branch). The integrate primitive serializes strategies per host repo with an async lock keyed on the resolved git common dir, so custom strategies inherit it. On conflict, integration aborts cleanly, the host repo is untouched, and the series is preserved on `waystation/<run-id>` — a branch created only on conflict and never deleted by waystation. Only `target=head` touches the working tree, so it demands a clean tree and refuses with a typed error otherwise.

## Considered options

- Three named branch strategies (`head` / `merge-to-head` / named branch), as charted — collapsed into the two orthogonal knobs.
- A conflict-resolution seam — rejected in ADR-0015: resolution is an ordinary run the flow script launches.

Decided in [wayfinder ticket 3](https://github.com/jeffrichley/waystation/issues/3).
