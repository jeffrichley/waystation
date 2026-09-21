---
status: accepted
---

# `run_agent` owns the Outcome contract, and builds its command at the call

`run_agent(sandbox, provider, prompt, outcome_type, ...)` takes a prompt, not an `AgentCommand`. It derives the JSON schema from `outcome_type`, refuses a type that is not object-shaped with `TypeError`, asks the provider for its command with that schema, and validates every report against the same type. The refusal and the provider's `command` happen when `run_agent` is **called**; awaiting what it returns execs the agent. The object-shape check has one implementation, `waystation._outcome.outcome_schema`, which `flow.run()` also calls so a bad type is still refused where the run is described (ADR-0021).

Why: the old primitive took the command, so its caller derived the schema and handed it to the provider, then passed the type separately for validation. Nothing tied the two together — the hand-composed loops in the tests passed `{}` as the schema — and the object-shape check lived only in `Flow`. ADR-0019 makes core the single judge of the Outcome; that only holds if the judge also chose the question.

Building the command at the call keeps a run's behaviour exactly as it was: a provider whose `command` raises never started an agent, so `Flow` fails the agent stage with `Errored` before `agent_start` is logged and without an `agent_end` firing. An `async def` could not say that — nothing in it runs until it is awaited, by which point `Flow` has already announced the agent. For a flow script that composes the primitives by hand the effect is the ordinary one for a bad argument: the error comes from the line that made the call, not from a later `await`.

## Considered options

- Keep `command` as a parameter and check it against the type. Rejected: an `AgentCommand` does not carry the schema it was built from, so there is nothing to check against.
- An `async def` that builds the command when awaited. Rejected: `agent_end` would then fire for an agent that never started, which changes what an awaited `RunSpec` does.
- A separate public `agent_command(provider, prompt, outcome_type)` step. Rejected: it adds to the public surface and gives the schema back to the caller, which is the gap this closes.

## Consequences

`run_agent` is a plain function that returns a coroutine. A caller that builds one and never awaits it gets Python's "never awaited" warning, as it would for any coroutine. `test_outcome_contract.py` holds the call-time behaviour.

Decided in [#80](https://github.com/jeffrichley/waystation/issues/80).
