---
status: accepted
---

# Flows are plain imperative Python with one parallel spelling

A flow script is Python the user owns: `Flow` holds shared defaults, `flow.run(agent)` opens a per-run builder, and `fan_out` is the only parallel construct — an async iterator over any iterable of runs, heterogeneous batches allowed, sequencing between fan-outs is ordinary `await`. There is no stage framework, no `flow.parallel`, no grouping vocabulary, and the API is async-first because parallel agent fan-out is the headline use case. Why: bareness is the prime directive, and a grouping construct is the first step onto the stage-framework slope. Revisit only if real flow scripts sprout a stage shape naturally.

## Considered options

- A stage / pipeline DSL the library interprets.
- `flow.parallel(...)` sugar over `fan_out`.
- A sync API over threads.

Decided in the charting session and [wayfinder ticket 4](https://github.com/jeffrichley/waystation/issues/4) (prototypes on `prototype/flow-api-shapes`).
