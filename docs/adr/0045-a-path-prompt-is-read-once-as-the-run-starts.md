---
type: adr
title: 0045 A Path Prompt Is Read Once As The Run Starts
status: stable
---

# A path prompt is read once, as the run starts, and the agent is handed that text

A prompt given as a `Path` is read as UTF-8 once per run, at the start of the lifecycle, before `run_start` fires and before the workspace stage. The text goes on the run record, and it is what `ctx.prompt` shows every hook and what the agent stage hands `provider.command`. The file is not read again during that run. Each await of a spec is a new run, so each await reads the file again. A file that cannot be read still fails the run with `stage="agent"`, reported after `run_start`, so a run never ends without starting.

Why: a hook sees only the `RunContext`, so a prompt that exists only once the agent stage starts is out of reach of every hook before it. Two built-in observers need it at `run_start`: `RunLogFiles` writes the full text at the head of each run's file (#34), and `EventLog` records its length. Reading once also means the prompt the log records is the prompt the agent got. A second read at the agent stage could hand the agent text the log never showed. The window this closes off is the workspace clone plus the sandbox start. It does not include time queued behind `fan_out`'s `max_concurrency`, because a queued run has not started: it takes its slot before `perform` begins, so it reads the file once the slot opens.

## Considered options

- **Read at the agent stage, as #18 first said.** Rejected: `ctx.prompt` would be empty for `run_start`, `workspace_ready` and `sandbox_ready`, so the two built-in observers above could not record it, and a user's hook bundle would have no way to either (ADR-0001: the built-ins use the user's protocol, so what they can't see, a user can't).
- **Make `ctx.prompt` lazy, read on first access.** Rejected: the moment of the read would become whichever hook touched it first, and so would the failure. A hook's exception is not an agent-stage failure, and a run's behaviour should not depend on which observers are attached.
- **Read twice: once for `ctx.prompt`, again for the agent.** Rejected: the log and the agent could disagree, and the second read adds a failure point for a file already shown to be readable.

## Consequences

An edit to the prompt file after a run has started, including while its workspace is cloning or its sandbox is starting, reaches the next run and not this one. #18's story 5 and its *Prompts* contract now say "read when the run starts" rather than "when the agent stage starts". `tests/test_path_prompt.py` holds both halves: an edit between two awaits of one spec is picked up, and edits made from `run_start`, `workspace_ready` and `sandbox_ready` hooks do not reach the agent. `tests/test_observers.py` still holds the failure as `stage="agent"`.

Records the move made in ad144fb, which had called it an implementation detail. It overrides the aside in [ADR-0018](0018-pure-agent-providers-bind-the-cli.md) that a prompt is "read when the agent stage starts"; that ADR's decision, about what a provider is, stands. Decided in [#143](https://github.com/jeffrichley/waystation/issues/143).
