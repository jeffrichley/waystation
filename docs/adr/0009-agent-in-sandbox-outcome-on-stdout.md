---
status: accepted
---

# The agent runs inside the sandbox and reports an Outcome on stdout

An agent is a subprocess inside the sandbox, never a host process; an agent provider is an argv/env builder plus an output parser. The prompt is piped on stdin, not passed as an argument (Linux argv tops out near 128 KiB; stdin is capped at 10 MB). The Outcome is a marker-prefixed JSON line on stdout, found by scanning from the end and validated by pydantic against an optional user-supplied schema. Claude Code is the first provider: `claude -p -` with `--output-format stream-json` or `--json-schema`, auth by env injection, run as a non-root user. Why: stdout is the one channel every sandbox backend already provides, so no shared filesystem or socket is needed, and reverse-scanning tolerates chatter after the marker. Consequence: console logging must use stderr (ADR-0001).

## Considered options

- An outcome file written into the workspace.
- A socket back to the host.

Decided in the charting session and [wayfinder ticket 8](https://github.com/jeffrichley/waystation/issues/8).
