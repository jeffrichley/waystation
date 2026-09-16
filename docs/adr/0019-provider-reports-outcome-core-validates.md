---
status: accepted
---

# The provider reports the Outcome; core validates it

This amends ADR-0009's contract. A marker-prefixed JSON line on stdout assumes a process that can print raw lines to the exec's stdout — true of foreman's wrapper, false of an agent CLI that owns stdout as its own event stream, where the model's text arrives nested inside JSON events. Each agent provider now delivers the run's JSON schema in its agent's native way and reports the Outcome from `parse` as an `OutcomeReported(raw)` event; core validates every report with pydantic into the run's outcome model, the last report wins, and the first starts the `completion_grace` clock (ADR-0017). Claude Code passes `--json-schema` and reports `structured_output` from its final `result` event — the CLI re-prompts the model until its output matches. Agents with no native schema output (Cursor, Gemini, Aider, Copilot) use two shipped pure helpers: `outcome_instructions(schema)`, appended to the prompt by the provider, and `find_outcome(text)`, which finds the marker line in agent text. The marker survives as a convention, not the contract. Against ADR-0016: no report and exit 0 is `OutcomeMissing`; a non-zero exit is `AgentExited` whatever was reported (Claude's `error_max_structured_output_retries` lands here, its `errors[]` in the stdout tail); a report that fails validation is `OutcomeInvalid(raw, error)`.

Why: native schema enforcement with in-agent retry beats prompt discipline wherever an agent has it, and core stays the single judge, so a flow script receives a typed instance whichever agent ran. Verified 2026-09-16 on Claude Code 2.1.273: `--json-schema` with `--output-format stream-json --verbose` and a plain-text prompt on stdin yields `structured_output` on the `result` event.

## Considered options

- A core-owned marker for every agent: core appends the instructions and scans decoded text. Rejected: throws away Claude's validation and retry, and core would template prompts (ticket 11: no templating in core).
- Trusting agent-side validation and skipping pydantic. Rejected: the flow script needs a typed instance, and third-party providers vary.

Decided in [wayfinder ticket 15](https://github.com/jeffrichley/waystation/issues/15).
