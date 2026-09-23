---
type: adr
title: A merge queue checks each landing on the target's head, and resolves at the front
status: stable
---

# A merge queue checks each landing on the target's head, and resolves at the front

`integrate` serializes the *write* to a target (ADR-0005, ADR-0033); it never re-checks the *combination*. Two series, each green against one base, can land one after the other and leave the target red. `merge_queue(repo, target, check=, sandbox=)` closes that gap: test each candidate applied to the target's tip exactly as it will land, and land exactly what was tested.

- **A candidate is a range on the host**, `submit(ref, base=)`: a run's preservation branch from its `base_sha`, or a branch a person pushed from its fork point. The series already lives on `ref`, so the queue never writes a preservation branch and never rewrites one. It is read when it reaches the front, with `PatchSeries.from_range`, which now refuses a range holding a merge commit, or one whose `ref` does not descend from `base`, as `Refused("nonlinear_series")` (ADR-0006). A forge's "Update branch" merge comes back as that refusal, never quietly flattened.
- **One target per queue, depth 1, arrival order.** No speculation: a caller at this scale lands single digits in bursts, and Uber's follow-up (arXiv:2501.03440) cut CI use by speculating *less*. Another order is [#139](https://github.com/jeffrichley/waystation/issues/139).
- **Rehearse, check, swap.** At the front, `Integration(target)` lands the series on a `GitRepo` whose `move_target` only notes where it would move the target, so the commit checked is the commit `integrate` would have landed, built by the same code and not a copy of it. `run_check` runs the check in a sandbox against a workspace cloned at that commit. Only a check that passed moves the target, through `integrate` and a strategy that only moves, whose compare-and-swap refuses a target someone else moved meanwhile (`Refused("target_moved")`). The repo's lock is held for the rehearsal and for the swap, never for the check: a test suite never blocks another target's landing or a run's preservation.
- **Results are values and nothing raises** (ADR-0007, ADR-0016): `Landed`, `LandingConflicted`, `CheckFailed` (with everything the check wrote), or `LandingFailed` for a refusal, git, the sandbox, or a resolver that raised. A failing candidate evicts only itself.
- **Resolution happens at the front, when the caller asks.** With `resolve=`, a conflict or a failed check is not an eviction yet: the queue hands the caller an `Attempt` and performs the `RunSpec` it returns, started at `attempt.start` (the head after a conflict, the commit that failed after a failed check) and landing nowhere. The branch that run keeps is the next round. `None` gives up, and the result carries every round. The queue counts rounds; the caller decides how many (ADR-0017).
- **`run_check` is public**, so a caller can run the same check against the bare target and tell "this candidate broke it" from "it was already red". That policy is the caller's.
- **The block** is `queue()`'s (ADR-0047): `submit` returns a handle whose `cancel()` stops that candidate alone; `close()` takes no more; leaving the block stops the candidate at the front, with its check's process tree killed and its workspace removed, and never starts the rest. The two queues share one private core, `_serving.Serving`, so the drain, the stop that waits and the cancelled wait that loses nothing are written once.

**Why at the front, not the back of the line.** A resolver run sent to the back works against a head that may move before it returns, so it can conflict again, and again: under load nothing guarantees progress. At the front the queue is serial, so nothing lands on the target while the resolver works, and its output re-applies onto that head by construction. A conflict cannot recur. What can recur is a failed check, and that loop costs money, so it is bounded by the caller's `resolve` returning `None`, never by a number the library picked. The price is head-of-line blocking while an agent resolves, which a caller landing single digits can afford for the guarantee.

**This does not reopen ADR-0015.** That ADR rejected a resolver inside `integrate` because it would hold the repo's lock for minutes and nest a sandbox inside the integrate stage. The queue holds no lock while a resolver runs, and the resolver is an ordinary run whose prompt the caller owns: nothing distinguishes it but that prompt.

## Considered options

- **`Validated(Integration(target), check=…)`, a strategy, with the caller writing the loop.** Rejected: a strategy runs inside `integrate`'s lock, so every landing and preservation in the repo would wait out a test suite.
- **A candidate as a `PatchSeries`.** Rejected: the queue would have to preserve what it could not land, and rebuilding onto an existing branch rewrites its shas. Every series a caller has is already on a ref.
- **Send a conflicted candidate back through the caller's own queue.** Rejected for the livelock above.
- **Skip the check when the candidate's tree was already checked** (a tree hash as proof). Deferred: a run's own final green is not something waystation observes, so the proof would only be the caller's claim. Always re-check until a caller measures the cost.
- **Push the landed branch to a remote.** Rejected: the caller pushes after `Landed` (ADR-0004).
- **Duplicate `Integration`'s replay with the public landing steps.** Rejected: two copies of the landing drift, and then the commit checked is not the commit `integrate` would land.

## Consequences

`waystation.__all__` gains `merge_queue`, `run_check`, `Landing` and its four kinds, and `Attempt`; `waystation.merge_queue` also exports `Candidate`, `CheckResult`, `Resolve`, and the `MergeQueue` and `QueuedCandidate` annotation aliases. #18's import surface has to name them. `PatchSeries.from_range` refuses a non-linear range where it used to flatten it. A candidate stopped by its handle or the block reports nothing, even in the one case where its swap had already begun and finished (ADR-0027): the caller reads the target. The check is unbounded, as nothing asked for a bound; a caller stops a hung one through the handle. `CONTEXT.md` gains **Merge queue**, **Candidate** and **Check**.

Decided in [#138](https://github.com/jeffrichley/waystation/issues/138).
