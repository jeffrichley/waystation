---
type: adr
title: 0048 A Queue Pulls By An Injected Ordering Strategy
status: draft
---

# A queue pulls by an injected ordering strategy, asked only when there is a choice

Which waiting run a queue starts next is an **ordering strategy** the caller injects: `queue(order=...)`. It is GoF Strategy, as integration is (ADR-0005). The default is `ArrivalOrder`, so a queue given none starts runs in the order they were submitted, as ADR-0047 says it does. The ticket's four open questions were settled like this:

- **Shape.** The strategy is a small protocol, `OrderingStrategy[ItemT]`, with one method: `pick(waiting) -> item`. It sees the whole waiting set, oldest first, and returns one of those items itself. A key function or a comparator is a special case of it (`min(waiting, key=...)`), and neither can look at the set as a whole.
- **When it is asked.** At every pull, over what is waiting then, never once at submission, so a ranking that changes as runs land is read as it is at the pull. It is asked **only when there is a choice**: when more runs are waiting than there is room for. A queue with no cap never asks it, and a run submitted into free room just starts. The pull after a submission waits one turn of the loop, so runs submitted together are ranked together. Six ready with room for three starts the best three, not the first three submitted.
- **One protocol or two.** One, generic over what the queue holds, in `waystation.ordering`. The run queue offers its waiting `QueuedRun` handles, whose `.spec` says which run each is. The merge queue ([#138](https://github.com/jeffrichley/waystation/issues/138)) offers whatever it holds and takes the same protocol.
- **Starvation.** Waystation guarantees nothing. A run ranked last for ever waits for ever. Ageing is policy, and ADR-0017 puts no policy in by default. A strategy that wants it keeps the arrival times itself. `ArrivalOrder` never starves anything.

**A strategy that fails is a failure, not a raise** (ADR-0016). When `pick` raises, or returns something that is not waiting, every run it was ordering at that pull is refused. Each becomes a `RunFailed` whose `stage` is `None`, with `failure=Errored(error)`, the same way a run that fails its preflight is refused (ADR-0047). The queue goes on taking runs. A run cancelled while waiting is never offered to the strategy.

`pick` is synchronous. The pull runs when a slot frees, and a slot left free across an `await` would be a race with the next submission. A ranking that needs I/O is read before the pull, into whatever the strategy holds.

**Why.** A caller often knows that some runs matter more than others, and Waystation cannot know why. Wayfarer's case is to start first the ticket whose landing unblocks the most other work, which is a fact about a ticket graph Waystation never sees. The strategy lets the caller supply the ranking while the queue keeps the cap, the pull and the stop. There are two named implementations from the start, arrival order and the caller's ranking, and two queues that take them.

## Considered options

- **A key function (`order=lambda run: rank[run]`).** Rejected. It ranks each item alone, so it cannot, for example, prefer whichever of two waiting runs touches fewer files than the other. Written as `pick`, it is one line.
- **Ask once, at submission.** Rejected. A ranking fixed at submission is stale by the time a run that waited hours is pulled, which is the case that matters.
- **Ask at every pull, even with no choice.** Rejected. A strategy asked to choose from one run can only say yes, or fail and refuse a run that had room. And a queue with no cap would ask it for nothing.
- **Pull at `submit`, synchronously.** Rejected. The first runs submitted in a burst would take the free slots before the rest were waiting, so a burst would be served in submission order whatever the strategy said.
- **Raise the strategy's error out of `__anext__`, or out of the block.** Rejected, as ADR-0047 rejected it for preflight: one bad pull would stop a consumer that is serving every other run.
- **Fall back to arrival order when the strategy fails.** Rejected. That traps the error, and the caller never learns that the ranking they supplied is not being used.
- **Two protocols, one per queue.** Rejected. They would have the same method over different items. A type parameter already says that.

## Consequences

`RunFailed` with `stage=None` now means "refused before it began" for one of two reasons, a failed preflight or a failed strategy. `failure` says which. The strategy is caller code running inside the queue's pull, so it must not block: it is called on the event loop. `OrderingStrategy` and `ArrivalOrder` live in `waystation.ordering` and are not in top-level `waystation.__all__`. A flow script that passes a strategy never needs to name the protocol, because the protocol is structural, and #18's import surface does not list it. `fan_out` takes no strategy. A batch starts in the order it was given, and a caller who wants another order sorts the batch.

Reached in [#139](https://github.com/jeffrichley/waystation/issues/139), by the implementing agent. It stays a draft until the user agrees it.
