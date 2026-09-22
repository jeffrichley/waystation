---
type: adr
title: 0044 The Scripted Agent Lives In Waystation Testing
status: stable
---

# `ScriptedAgent` lives only in `waystation.testing`, and the suite beside it loads lazily

`ScriptedAgent` and `ScriptedCommit` are exported from `waystation.testing` and nowhere else: `waystation` and `waystation.agents` no longer export them, and the module moved with them, from `waystation/agents/scripted.py` to `waystation/testing/scripted.py`. `SandboxConformance` stays in the same package but is resolved by a module `__getattr__`, so importing the package, or `ScriptedAgent` from it, never imports pytest. Only touching the suite does, and pytest is still the `waystation[testing]` extra.

Why: #18's public import surface names `waystation.testing` as `ScriptedAgent`'s one home, and story 114 promises it there. Neither `waystation` nor `waystation.agents` lists it. It sat in both because it was built before `waystation.testing` existed, and ADR-0035 put off the move, stating the condition for making it: a flow-script author testing with `ScriptedAgent` must not need pytest just because the conformance suite shares the package. A test double exported beside `ClaudeCode` says it is a production agent. Two exported spellings of one name leave every reader to decide which one is real.

## Considered options

- **Keep `waystation.ScriptedAgent` and `waystation.agents.ScriptedAgent` as aliases.** Rejected: nothing breaks, but the surface keeps two names #18 does not list, and the aliases would have to be defended every time the surface is read. waystation is at v0 with no users outside this repo, so this is the cheapest point to remove them.
- **Import the suite eagerly, and make pytest a runtime dependency.** Rejected: #18 limits runtime dependencies to pydantic and rich.
- **Split the suite into its own package, `waystation.testing.conformance`, as the only path.** Rejected: `waystation.testing.SandboxConformance` is the path ADR-0035 published and `tests/CLAUDE.md` documents, and a lazy attribute keeps it at no cost.

## Consequences

Anyone importing `ScriptedAgent` or `ScriptedCommit` from `waystation` or `waystation.agents` gets an `ImportError` and must switch to `from waystation.testing import ScriptedAgent`. `tests/test_testing_package.py` holds the lazy load: in a subprocess where pytest cannot be imported, `ScriptedAgent` imports and `SandboxConformance` raises `ModuleNotFoundError` for pytest.

Extends [ADR-0035](0035-the-host-process-runner-is-public-and-a-suite-states-the-exec-contract.md) by making the move it deferred. Decided in [#142](https://github.com/jeffrichley/waystation/issues/142).
