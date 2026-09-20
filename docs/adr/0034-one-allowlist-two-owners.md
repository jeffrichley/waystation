---
status: accepted
---

# One allowlist, two owners: core resolves its tiers, a backend resolves its own

`allowlisted_env(literal=..., pass_env=..., base=...)` becomes public in `waystation.sandbox` and is the only place an environment is built (ADR-0013). Core calls it for the tiers it owns — the agent provider's `env`/`pass_env` and, once [#29](https://github.com/jeffrichley/waystation/issues/29) lands, the per-run ones — and hands the result to `start` and `exec` as literals; a backend calls it for the tiers it owns, its own spec `env`/`pass_env` and the base keys its execs need on this OS. `run_agent` stops hand-rolling a loop over `os.environ`. Within any tier a **literal beats a pass-through name**: `env={"FOO": "x"}` is the most explicit thing a user wrote, while a pass-through name only says "bring whatever the host has". The host environment is read **once per owner**: core reads it once for the tiers it owns and reuses that reading for everything it resolves, and a backend reads it once inside `start` for its own. The helper never reads it — `host_env` is passed in — so each owner decides how far one reading stretches, and a run cannot straddle two of them within a tier. A pass-through name the host does not have is skipped, and the skipped *names* are logged at DEBUG — never values (ADR-0025).

`SandboxBackend.start` loses its `pass_env` parameter: core hands over resolved literals, and a backend's own pass-through lives on the backend. What reaches which call:

| Tier | Sandbox start | Agent exec | Other execs (`clone_in`, collect) |
| --- | --- | --- | --- |
| Sandbox spec (`DockerSandbox(env=...)`) | yes | yes | yes |
| Agent provider (`ClaudeCode(env=...)`) | no | yes | no |
| Per run ([#29](https://github.com/jeffrichley/waystation/issues/29)'s `.env()`) | yes | yes | yes |

So a provider's credential never reaches collect or `clone_in`, while a per-run value does, because it goes in at the start.

Why: the same logic lived twice and the two copies disagreed. `allowlisted_env` put literal values on top of pass-through ones; `run_agent` wrote the provider's literals first and then overwrote each from `os.environ`, so the same two tiers ranked differently depending on which path built the environment. ADR-0013's order — spec, then provider, then per run — was stated in #18 but implemented in three places (the orchestrator, `run_agent`, and each backend's merge), which is how #29 would have got it subtly wrong. Ownership is the line that keeps one implementation from becoming a protocol change: a `DockerSandbox(env=...)` is the user's configuration *on the backend*, and only the backend knows which base keys its own execs need — `NoSandbox` needs `PATH`, `HOME` and `TMPDIR` because it runs host processes, while Docker passes none, since the container brings its own.

## Considered options

- **Core resolves every tier**, with the protocol exposing each backend's `env`/`pass_env`. Rejected: core reaching into backend configuration, and a protocol that has to describe the backend's own fields.
- **Leave the two implementations and document the order.** Rejected: that is today, and the two already disagree.
- **Pass-through beats literal.** Rejected: naming a key twice should resolve to the value the user typed, not to whatever the host happens to hold.
- **Fail when a pass-through name is missing.** Rejected: `pass_env=("CI",)` would then be unusable on a laptop.
- **Read `os.environ` per call.** Rejected: a host process that mutates its environment mid-run would give a run two different environments.
- **One reading for the whole run, backend included.** It would have to reach the backend, and `start(ws, *, env)` carries literals by the decision above — a third parameter for it is the kind of protocol widening this ADR exists to avoid. Two readings stand instead, one per owner, ordered: core's, then the backend's inside `start`. A test holds the thing that actually matters — that a sandbox and its agent never disagree about a key.
- **Keep `pass_env` on `start()` and pass `()` forever.** Rejected: a parameter that only ever receives one value is a lie in the protocol a third backend reads.

## Consequences

`run_agent`'s precedence changes, silently, for anyone who named the same key as both a literal and a pass-through: a test pins the new rule. #18 gains the tier table and an edited sandbox contract (`start(ws, *, env)`), written when the code matches. [#75](https://github.com/jeffrichley/waystation/issues/75)'s conformance suite gets one rule to state — a backend merges the literals it is given over its own tier — and [#77](https://github.com/jeffrichley/waystation/issues/77) touches the same signatures, so it should land after. #73 tests the spec and provider rows; #29 wires its own tier and adds that row rather than #73 implementing half of it.

Decided while grilling [#73](https://github.com/jeffrichley/waystation/issues/73), out of the architecture review in [#69](https://github.com/jeffrichley/waystation/issues/69). Amended while implementing it: "once per run" was written as though core's reading reached the backend, which the protocol this ADR chose gives it no way to do. It is once per owner.
