---
status: accepted
---

# The sandbox environment is cleared and allowlisted

Nothing from the host environment reaches a sandbox unless named: literal `env={...}` and pass-through `pass_env=[...]` resolved from the host at launch, with precedence sandbox spec → agent provider → per-run override. `NoSandbox` applies the same policy so dev and test behaviour match Docker, and values are never logged (ADR-0001). Why: agent credentials are the only secrets in play, and an allowlist is the only posture where a new host variable cannot leak by default.

## Considered options

- Inherit the host environment with a denylist.

Decided in [wayfinder ticket 5](https://github.com/jeffrichley/waystation/issues/5).
