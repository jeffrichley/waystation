---
type: project
status: deprecated
---
# PROTOTYPE — flow-authoring API shapes

**Throwaway. Not runnable — waystation doesn't exist yet.** Reaction artifact
for [ticket #4](https://github.com/jeffrichley/waystation/issues/4): what
should authoring a flow *feel* like?

Three rival scripts, **identical scenario**, only the API shape differs:

> Fan out three Claude Code agents against this repo (base `main`), each in a
> Docker sandbox, each reporting a typed `TriageOutcome`. Merge each patch
> series into branch `agents/triage` as runs complete. Hooks announce sandbox
> startup and integration. Conflicts and failures handled per-run.

| Variant | Run setup | Hooks register | Flavor |
|---|---|---|---|
| [A — plain](variant_a_plain.py) | `Run(...)` value object, all kwargs | `Hooks(...)` field on the run | barest; a run is inspectable data |
| [B — builder](variant_b_builder.py) | fluent chain off `waystation.run(repo)` | chained `.on_*()` methods | reads top-to-bottom like a sentence |
| [C — flow object](variant_c_flow_object.py) | shared `Flow` holds common config; `flow.run(agent)` per run | `@flow.on_*` decorators, once for the whole batch | separates "what's shared" from "what varies" |
| [D — hybrid](variant_d_hybrid.py) | `Flow` = defaults + `flow.run(agent)` opening a fluent builder with per-run overrides | both levels: `@flow.on_*` decorators fire first, then the run's chained `.on_*()` | round 2: heterogeneous parallel batch (research + test agents mixed), phase sequencing in plain Python |

## Round-1 reactions (recorded)

Builder liked; decorator hooks "spiffy"; kwargs fine → D hybridizes all
three. Hooks wanted **at each level** → D layers flow-level + per-run.
Hook vocabulary and typed result union approved. Builder copy-vs-mutate
deferred as weeds. New concern → round 2: a flow must run **mixed items
in parallel** (several review/research agents at once) — C's single
shared config fought that; D makes the heterogeneous batch the headline.

## React to (round 2)

1. Does D's mixed `fan_out` batch scratch the "parallel items in a flow"
   itch, or do you want a first-class grouping construct
   (`flow.parallel(...)`)? Bareness lens: `fan_out` + plain `await`
   already express it; a construct edges toward the deferred stage
   framework.
2. A run with no `.integrate(...)`: skip integration entirely, or fall
   back to a default (head + apply)? Surfaced by outcome-only research
   runs.
3. Hook layering rule (flow hooks first, then the run's own — both fire,
   no shadowing): right call?

## React to (round 1 — settled)

1. **Which reads best at the call site** — the `make_run()` body in A vs B, and
   C's split of shared vs per-run?
2. **Hook granularity**: per-run (A, B) or per-flow (C)? If per-flow, does a
   single run still need an override path?
3. **Builder semantics** (if B): each `.x()` returns a copy (safe to fork a
   half-built run) or mutates self (cheaper, but aliasing surprises)?
4. **Hook naming**: `on_sandbox_ready` / `on_integrated` — right vocabulary?
   Full candidate set: `run_start`, `workspace_ready`, `sandbox_ready`,
   `agent_end`, `integrated`, `run_end`.
5. **Result consumption**: the `match` over `RunSucceeded / RunConflicted /
   RunFailed` is identical in all three — does that typed union feel right?

Non-goals here: CLI ergonomics (the flagship example being a typer script is
already settled in #2), exact Outcome/report field names (#4 hand-off from #3
covers shapes, but field-level design sharpens at spec assembly).
