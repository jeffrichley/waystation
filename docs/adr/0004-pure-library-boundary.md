---
status: accepted
---

# Waystation is a pure library: no CLI, no forge, no magic directory

Waystation ships no CLI and no typer dependency, speaks only git — no GitHub, GitLab or Bitbucket abstraction — and reads no `.waystation/` directory: prompts are Python strings, configuration is constructor arguments. `examples/` teaches usage, and the flagship example is itself a typer script the user copies and owns, so every flow starts life with a CLI without the library owning one. Why: forge operations and CLI ergonomics are flow-script territory, and a forge abstraction would be built for the one forge in use while dragging the library toward a framework.

## Considered options

- A forge adapter seam in core.
- A `waystation` command.
- A project-local config directory like sandcastle's `.sandcastle/`.

Decided in the charting session and [wayfinder ticket 2](https://github.com/jeffrichley/waystation/issues/2).
