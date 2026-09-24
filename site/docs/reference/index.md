# API reference

Generated from the code: each page documents exactly the names its module
lists in `__all__`, which is the public surface's contract. Anything not here
is private, however importable it is.

**The top level is what a flow script writes and reads.** Build a `Flow`,
describe runs, await them, match on results, register hooks, compose the
primitives by hand, turn on observability.

**A seam's own vocabulary lives in that seam's module**, where the author
implementing it is already looking:

| Module | For writing |
| --- | --- |
| [`waystation`](waystation.md) | a flow script |
| [`waystation.agents`](agents.md) | an agent provider |
| [`waystation.sandbox`](sandbox.md) | a sandbox backend |
| [`waystation.integration`](integration.md) | an integration strategy |
| [`waystation.ordering`](ordering.md) | an ordering strategy, which picks what a queue or merge queue pulls next |
| [`waystation.queue`](queue.md) | a flow that types a queue's handle, or orders its runs |
| [`waystation.merge_queue`](merge_queue.md) | a flow that submits candidates to a merge queue and resolves what stops one |
| [`waystation.stages`](stages.md) | a loop composed by hand, typed with its stage runner |
| [`waystation.testing`](testing.md) | tests: a scripted agent, and a backend's conformance suite |

A module also re-exports what a flow script hands it, so `DockerSandbox`
appears under both `waystation` and `waystation.sandbox`, and `merge_queue`
under both `waystation` and `waystation.merge_queue`.
