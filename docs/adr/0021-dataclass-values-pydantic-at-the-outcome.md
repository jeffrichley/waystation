---
status: stable
type: adr
---

# Waystation's values are dataclasses; pydantic validates only the Outcome

Every value waystation constructs — results, failures, integration reports, `Timeouts`, `AgentCommand`, agent events, sandbox specs — is a frozen stdlib dataclass (`frozen=True, slots=True`). Pydantic, a hard dependency, appears at exactly one boundary: validating the Outcome an agent reports. A run's `outcome=` accepts any object-shaped type pydantic's `TypeAdapter` handles — a `BaseModel`, a stdlib dataclass or a `TypedDict` — with the JSON schema taken from `TypeAdapter(T).json_schema()`. Non-object types are rejected at `flow.run()` with `TypeError`; the default is a shipped `Summary(summary: str)`.

Why: third-party agent providers and sandbox backends construct these values and should need no pydantic knowledge — ADR-0018 already hands providers the schema as a plain dict for that reason. Positional `match RunFailed(stage, failure)` patterns need `__match_args__`, which dataclasses generate and `BaseModel` does not. Nothing waystation builds itself is untrusted; the agent's report is, so validation lives there. `EventLog` serialization still comes free, since `TypeAdapter` dumps dataclasses. The split is OpenAI Agents SDK's: `RunResult` is a dataclass, and `output_type` is any `TypeAdapter`-able type.

## Considered options

- Frozen pydantic models everywhere — the user's own preference for data objects. Rejected for the provider-author and pattern-matching costs above.
- Requiring `BaseModel` outcomes.
- Wrapping non-object outcome types (`str`, `bool`, `list[str]`) in a `{"response": ...}` envelope, as OpenAI Agents does. Deferred: `Summary` covers plain text, and wrapping is additive.

Decided in [wayfinder ticket 7](https://github.com/jeffrichley/waystation/issues/7).
