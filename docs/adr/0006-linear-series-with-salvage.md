---
status: accepted
---

# The patch series is linear and salvaged by default

A run's product is a linear patch series atop its base ref — `git format-patch` out of the sandbox, `git am --3way` semantics into the host (landed by plumbing, ADR-0020) — so agent-created merge commits are unsupported in v0 and an empty series is valid, not an error. At run end waystation commits whatever the agent left uncommitted as a `WIP` salvage commit (`.gitignore` respected) that rides the series and is flagged on the run's result (moved off the integration report in wayfinder ticket 7, since a run with no integration still has a series); the knob is `salvage|drop`, default salvage. Why: patches are the one transport that works for every workspace transport (ADR-0012), and the failure modes are asymmetric — a visible junk commit under salvage versus silent loss of real work under drop.

Decided in [wayfinder ticket 3](https://github.com/jeffrichley/waystation/issues/3).
