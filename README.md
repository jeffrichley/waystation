# Waystation

A bare Python library for orchestrating sandboxed AI coding agents against git
repositories.

**Your flow is a Python script you own.** There is no config file, no DSL, no
CLI and no framework to fit into. Waystation gives you primitives: a run puts
one agent in a fresh sandbox against a clone of your repo, collects the
commits it made, and lands them where you ask. How many runs, in what order,
on which branches, and what to do when one fails are all ordinary Python you
can read, change and debug. When a run fails, you get the failure back as a
value to `match` on, not an exception to catch.

## The flagship, in about 20 lines

Fan three prompts out to Claude Code, each in its own Docker container, landing
every agent's commits on one shared batch branch while a live dashboard shows
progress:

```python
import asyncio

from waystation import (ClaudeCode, Dashboard, DockerSandbox, Flow, Integration,
                        RunConflicted, RunFailed, RunSucceeded, Summary, Timeouts,
                        configure_logging, fan_out, handle_signals)

PROMPTS = ["add type hints to utils.py", "write tests for the parser", "fix the flaky CI step"]

async def main() -> None:
    configure_logging()
    handle_signals()
    flow = Flow(".", agent=ClaudeCode(), sandbox=DockerSandbox("waystation-dev"),
                integration=Integration("agents/batch"), timeouts=Timeouts(agent_wall=1800))
    async with Dashboard() as dashboard:
        runs = (flow.run(p, outcome=Summary).hooks(dashboard) for p in PROMPTS)
        async for result in fan_out(runs, max_concurrency=2):
            match result:
                case RunSucceeded(outcome=outcome): print("landed:", outcome.summary)
                case RunConflicted(preserved=branch): print("conflict, kept on", branch)
                case RunFailed(stage=stage, failure=failure): print(stage, failure)

asyncio.run(main())
```

The full version, with a typer CLI and resolver runs that replay conflicting
work onto the batch branch, is rung 6 of the [examples](examples/README.md).

## Prerequisites

- **Python 3.12+** and [uv](https://docs.astral.sh/uv/).
- **Docker**, running. Waystation never builds or pulls an image. You build
  the examples' image once, with [just](https://just.systems/):

  ```sh
  just example-image
  ```

- **A Claude credential** in your shell, passed into the sandbox by name:

  ```sh
  export ANTHROPIC_API_KEY=...   # or CLAUDE_CODE_OAUTH_TOKEN (`claude setup-token`)
  ```

Then start with the [examples](examples/README.md), a seven-rung ladder that
adds one idea per rung.

## Reading further

`CONTEXT.md` defines the vocabulary, `docs/adr/` records every decision and
why, and `docs/log-records.md` says what a run logs and how to watch it.
