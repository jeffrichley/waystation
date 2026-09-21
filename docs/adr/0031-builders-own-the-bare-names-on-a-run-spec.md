---
status: stable
type: adr
---

# A run spec's builder methods own the bare names; its stored values take the glossary's

On `RunSpec`, the chained builder methods #18's authoring contract names — `.agent(p)`, `.sandbox(s)`, `.base(ref)`, `.timeouts(t)`, `.salvage(bool)`, and `.env()`, `.pass_env()`, `.name()`, `.extra_refs()` as their tickets land — own the bare names. The values they set are stored under the words `CONTEXT.md` already uses for them: `provider`, `backend`, `base_ref`, `bounds`, `salvaging`, and later `environment`, `pass_through`, `label`, `visible_refs`. `repo`, `prompt`, `outcome_type`, `integration` and `hook_registry` keep their names, because no builder claims them. Those attributes are public and read-only — a scheduler may group runs by `spec.backend`, and nothing may write them — while *constructing* a `RunSpec` is not public: a spec comes from `flow.run()`, so a later ticket adding a field is not a breaking change. A builder that sets a value replaces it, as `dataclasses.replace` does; the hook builders (`.hooks()`, `.on_<hook>()`) append. `with_timeouts` is deleted rather than aliased, and a test holds the rule by refusing any public method whose name is also a field's.

Why: the collision was already deciding the API one ticket at a time. `RunSpec` is a frozen dataclass, so its fields took `agent`, `sandbox`, `base`, `timeouts` and `salvage` first, and `.timeouts(t)` had to ship as `with_timeouts` "so it does not shadow the `timeouts` field" ([#26](https://github.com/jeffrichley/waystation/issues/26)). [#29](https://github.com/jeffrichley/waystation/issues/29) (`.env()`, `.pass_env()`, `.name()`) and [#32](https://github.com/jeffrichley/waystation/issues/32) (`.extra_refs()`) would each have hit the same wall and each picked their own spelling, leaving a surface that is half bare and half `with_`. The builders keep the bare names because that is what a flow script reads — `flow.run(p).agent(ClaudeCode()).base("main")` — and because generative methods under bare names is the shape of SQLAlchemy 2.0's `select()`, the precedent ADR-0022 already took for immutable specs. The stored values keep public names, rather than going private, because core reads them: `_preflight` dedupes a batch by `spec.provider` and `spec.backend`, and privatising them would make the library reach across modules into a private attribute — the privileged internal path the architecture notes say is the signal to stop. Taking those names from the glossary costs nothing: **agent provider**, **sandbox backend** and **base ref** are what `CONTEXT.md` calls them anyway.

## Considered options

- **Stored values go private (`_agent`, `_sandbox`), builders keep the bare names.** Rejected: `_preflight` and any future scheduler would read a private attribute across modules, and a spec's configuration stops being inspectable at all.
- **Fields keep the bare names and every builder becomes `with_*`.** Rejected: it amends #18's authoring contract, and it is not uniform either — `.integrate()`, `.hooks()` and `.on_<hook>()` stay bare verbs, so the rule would be "nouns get `with_`, verbs don't".
- **A separate builder object that produces a plain value.** Rejected: two types for one concept, against ADR-0022's one frozen value, and a flow script would have to know which one it holds.
- **Aliases for a release, then removal.** Rejected: version 0.1.0, nothing published, every caller in this repo. Pre-1.0 is when a rename is free.
- **`stage_bounds` instead of `bounds`.** Weighed and dropped: longer, and ADR-0017 says "bound" throughout.

## Consequences

`Flow` is untouched: it has no builders, and its constructor keywords are fixed by #18. So `flow.agent` and `spec.provider` name the same thing differently — the one wart this decision accepts, in exchange for leaving a spec's readers on public names. #29 and #32 inherit the rule rather than deciding it: each adds its builder under the bare name and its value under the glossary's, and each brings the part of the "every builder #18 lists exists" test its own methods make true. #18's authoring contract gains a line naming the read-only attributes, which is a clarification, not a change: the contract always showed bare-name builders.

Decided while grilling [#82](https://github.com/jeffrichley/waystation/issues/82), out of the architecture review in [#69](https://github.com/jeffrichley/waystation/issues/69).
