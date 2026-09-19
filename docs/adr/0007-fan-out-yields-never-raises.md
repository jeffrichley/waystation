---
status: accepted
---

# Fan-out yields typed results as runs complete and never raises

`fan_out` starts every run and yields each result as it completes — `RunSucceeded` / `RunConflicted` / `RunFailed` — with no cancel-on-failure and nothing raised mid-iteration; an optional concurrency cap, unlimited by default. Leaving early is structured: `async with fan_out(batch) as results` cancels in-flight runs on exit — shielded salvage, preservation and teardown per ADR-0017 and ADR-0023 — and never starts queued ones, while a bare `async for` stays the spelling for consuming a whole batch ([wayfinder ticket 7](https://github.com/jeffrichley/waystation/issues/7)). Why: the flow script owns the parallelism, so it owns retry and abort policy, and it can only do that if every run finishes and reports. Gather-all is a list comprehension, not a second API.

## Considered options

- An async generator whose close cancels the rest, so a `break` in a bare `async for` stops the batch. Rejected: the close runs whenever the generator is collected, so the consumer cannot know when the batch has wound down — the uncertainty the block exists to remove.
- Stopping the batch when a wait for its next result is cancelled. Rejected: `asyncio.wait_for(anext(results), …)` giving up would end every run. A cancelled wait gives its place back instead, and its result goes to the next one.

## Consequences

Leaving a bare `async for` early stops nothing, as with `asyncio.as_completed`: the runs left go on unobserved until they end, or until `asyncio.run` cancels them as it closes the loop, when each still keeps its work. The block's exit, like `asyncio.TaskGroup`'s, returns only once every run it cancelled has wound down; a cancellation of the consumer meanwhile waits for that too and is raised afterwards. A run the exit stopped reports nothing, even to a reader who iterates afterwards: there is no cancelled result (ADR-0017).

The batch is preflighted once, and a run in it never checks again, so a queued run that starts late is not re-preflighted: an image removed in between is refused when the sandbox starts (ADR-0011). A lone awaited run is a batch of one, through the same check. Specs dedupe by equality, not hashing, since a spec holding a mapping has no hash. A `max_concurrency` below 1 is a `ValueError` at the call, since a cap of 0 would wait forever.

Decided in [wayfinder ticket 3](https://github.com/jeffrichley/waystation/issues/3). What leaving early does, in each form, was settled in [#31](https://github.com/jeffrichley/waystation/issues/31).
