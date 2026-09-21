---
status: stable
type: adr
---

# One run record sits behind `ctx`, and `ctx` carries the stage; a hook point stays a five-place change

A run's facts live in one private record, `waystation._run_record.RunRecord`. It holds the id, name, repo, prompt, base sha, live sandbox, current stage, per-stage elapsed, the agent's exit, the series, where it was kept or landed, and the one failure the run will report. `RunContext` is a read-only view over it, and every kind of result is assembled from it by one `_facts()` step. What used to be split across `RunState` (read by hooks), `_RunRecord` (read by results) and the view has one writer, the orchestrator, and nothing is written twice. `name` now reaches a result from the record rather than a `None` typed four times, so #29 only has to set it once.

`RunContext` gains two properties:

- **`ctx.stage`** is where the run is. The orchestrator moves it as each stage begins, integrate included. It is also the stage a hook raising right now is charged to: `HookRegistry.fire` no longer takes a stage, and reads `ctx.stage` instead, so the two cannot disagree. At `run_end` a failed run's stage is the stage it failed in. Collecting after a failed agent is work the run still owed, so the run did not end at collect.
- **`ctx.elapsed`** is the stage runner's own live, read-only mapping: seconds per stage whose work has ended. The result's `elapsed` is a copy of it at the end.

`ctx` is live. Read again later, it answers for the run as it stands then. `Dashboard` depends on that: a row holds its run's `ctx`, and the display reads `ctx.stage` as it draws. The row shows integrate while the series is landing, a stage no hook announces, and nothing in the dashboard infers a stage from which hook fired last.

**Adding a hook point still touches five places:** `HookName`, `HookBundle`, a `Flow` decorator, a `RunSpec` builder, and the orchestrator that fires it. (`RunLog` makes six, since it is the built-in bundle that logs each one.) This ADR decides that this stays. Instead of making hook points cheaper to add, `ctx` is what makes them less often needed: an observer that wants to know the stage reads it, where before it needed a boundary to hang the inference on. `test_a_hook_point_is_offered_everywhere_a_hook_is_registered` checks that every `HookName` has its method on `HookBundle`, `Flow`, `RunSpec` and `RunLog`, so a new one that misses a place fails a test rather than a user; that the orchestrator fires it is left to the lifecycle tests.

Why: the stage was known inside the run and hidden from every observer. The Dashboard inferred it from hook boundaries, so it never saw integrate begin. A user's bundle, being the Dashboard's peer (ADR-0001), was blind the same way. The facts were split so that `base_sha` had to be written into two objects, and a result's `name` could only ever be `None`.

## Considered options

- **Hook points for the stages that have none: `integrate_start`, `preserved`, `teardown`.** Rejected: each costs the five places and one more name for a user to learn, only to report a stage change. A single live `ctx.stage` covers every one of them, including stages added later.
- **Generate the per-hook decorators and builders from `HookName`, or offer one generic `on(hook, fn)`.** Rejected: each decorator and builder is typed for its own hook's arguments (`fn(ctx, line)`, `fn(ctx, exit)`, `fn(ctx, report)`), and a type checker uses that to catch a function registered at the wrong hook. Generated or generic registration makes that type `Callable[..., object]`. Adding a hook point is rare and costs five short edits, and the test holds them together. Losing the type check would cost every user every time.
- **Let the stage runner track the current stage.** Rejected: the runner sees only the work it is handed. It never sees the prompt being read or a hook firing, and it runs owed work under an earlier stage's name: a sandbox torn down after collect, or a series kept under `integrate` when the run integrates nowhere. A stage read off the runner would move backwards at teardown. Where the run is counts as policy, and ADR-0032 leaves policy with whoever composes.
- **Hand hooks a snapshot of the stage, and let the Dashboard update its row from it at each hook.** Rejected: that is the old inference with a better source. The row would still sit on `collect` until `integrated` fired.
- **Give `RunContext` a public constructor, so an observer can be driven outside a run.** Not done here: it commits the import surface to a way of building one, and #18's list names none. The stage runner fires no hooks by design (ADR-0032), so a hand-composed loop has no hook to hand a context to in any case. Tests that drive an observer directly build a `RunRecord` from the private module, as they built `RunState` before.

## Consequences

`ctx.stage` has a precise meaning where a hook fires and a looser one between hooks. It moves to a stage just before the work starts, and it does not move for owed work run under an earlier stage's name, such as teardown and preservation. A display reading it live shows the stage the run is in, not a line for each piece of owed work. `ctx.elapsed` lists a stage only once that stage's work has ended, so during the agent stage an `agent_output` hook finds no `agent` key.

Decided in [#79](https://github.com/jeffrichley/waystation/issues/79), out of the architecture review in [#69](https://github.com/jeffrichley/waystation/issues/69).
