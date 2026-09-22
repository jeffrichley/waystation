---
type: adr
title: Flows are plain imperative Python, and a queue is fan-out's open-ended peer
status: stable
supersedes: adr/0003-flows-are-plain-python
---

# Flows are plain imperative Python, and a queue is fan-out's open-ended peer

A flow script is Python the user owns. `Flow` holds shared defaults, `flow.run(...)` opens a per-run builder, and sequencing is ordinary `await`. There is no stage framework, no `flow.parallel` and no grouping vocabulary, and the API is async-first. All of that stands from ADR-0003. What changes is its *"`fan_out` is the only parallel construct"*. There are now two, told apart by lifetime rather than by grouping:

- **`fan_out(runs)`** is a **batch**. It reads its whole iterable, preflights it once, and ends when every run has reported.
- **`queue()`** is a **queue**, a service that stays open. `runs.submit(spec)` takes a spec whenever one becomes startable, from any flow and against any host repo. `runs.close()` says no more is coming, and iteration ends once everything submitted has reported. Until then, an idle queue waits for the next submission.

Every guarantee ADR-0007 gives a batch, a queue keeps. There is one optional `max_concurrency` across everything submitted, unlimited by default (ADR-0017), and queued runs start in the order they were submitted. Each typed result is yielded as its run ends, with no cancel-on-failure and nothing raised mid-iteration. The block is structured: leaving `async with queue() as runs` closes the queue, cancels the runs in flight with shielded salvage, preservation and teardown, returns only once each has wound down, and never starts the queued ones.

A queue adds three things a batch has no need for:

- **Each run is preflighted as it starts**, not when it is submitted, so a queue that runs for hours checks each run against the host as the host is then. For a queue, that answers ADR-0007's *"a queued run that starts late is not re-preflighted"*. A run that fails its preflight is one more result, since the queue never raises. It is a `RunFailed` whose `stage` is `None`, because it never began. `failure` is the `PreflightError`'s own failure, or `Errored` holding the error when it carries none. The run id is minted for an attempt that got no further. `RunFailed.stage` widens to `Stage | None` for it, as `StageError.stage` already had (ADR-0032).
- **`submit` returns the run**, and `await run.cancel()` stops that one alone. In flight, it keeps its work and tears down (ADR-0017). Still queued, it never starts. Either way it reports nothing, and every other run goes on. A run that has already ended is left as it was. `cancel` returns once the run has wound down, for the reason the block does: a caller that cannot know when a stop has finished cannot act on it. This is not the per-run `ctx.cancel()` ADR-0017 deferred. The caller holds the handle, and a hook does not.
- **`submit(spec, preflighted=True)`**, for a spec whose batch was already checked, as `spec.perform(preflighted=True)` does (ADR-0032).

`fan_out` is written on the queue, through that same public call: preflight the batch whole, submit every spec `preflighted=True`, close. When the library does what a user could do, it uses the user's protocol, so a scheduler of the user's own built on `queue()` is the same shape as fan-out.

**Why.** A caller whose work arrives over time had two poor options. The first was batches in waves, where the grouping means nothing, the cap applies per wave rather than overall, and stopping means tracking every wave. The second was a hand-rolled scheduler on `preflight` and `perform(preflighted=True)` that rebuilds the cap, the as-completed stream and the structured stop fan-out already gets right. Any long-lived service built on Waystation has this shape: Wayfarer working a ticket graph to completion, a bot, a CI watcher, a queue consumer. ADR-0003 rejected `flow.parallel` because it was grouping sugar over `fan_out`, the first step onto the stage-framework slope. A queue is not grouping: it has a different lifetime, and a batch is the special case of it that is closed at once.

## Considered options

- **Leave it in `examples/`: a hand-rolled scheduler on the ADR-0032 primitives.** Rejected. The cap, the result stream, the drain on close, the stop that waits for wind-down and the cancelled wait that loses nothing are each a place to get it subtly wrong. They are the same places fan-out already gets right, and every service built on Waystation would rewrite them.
- **Preflight at `submit`, making it async and raising `PreflightError`.** Rejected. A run submitted hours before it starts would be checked against a host that has since changed.
- **Raise `PreflightError` from `__anext__`.** Rejected. That breaks "never raises" (ADR-0007), and one bad spec would stop a consumer that is serving every other run.
- **An idle queue ends iteration.** Rejected. It races a submitter who is about to submit the work a result just unblocked. Explicit `close()` follows `asyncio.Queue.shutdown` and trio's channel close.
- **Defer cancel-one to a later ticket.** Rejected. Wayfarer's "stop this ticket" needs it, and the task that the handle cancels already exists.

## Consequences

`RunFailed.stage` is `Stage | None`, so a `match` that reads it handles `None`. Only a queue produces it, and a run that began always names a stage. `waystation.__all__` gains `queue`, and `waystation.queue` exports `RunQueue` and `QueuedRun` as annotation aliases, as `waystation.stages` exports `StageRunner`. #18's import surface has to name `queue`. `CONTEXT.md` gains **Queue**. An ordering strategy for which queued run starts next is [#139](https://github.com/jeffrichley/waystation/issues/139). The default stays arrival order.

Decided in [#113](https://github.com/jeffrichley/waystation/issues/113), superseding ADR-0003.
