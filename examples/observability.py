"""Rung 2: watching a run with hooks, ``ctx.log`` and an ``EventLog``.

**What it teaches.** Rung 1's run, now with the lights on. Waystation prints
nothing on its own: everything you see comes from something you added, and
there are exactly three kinds of thing to add.

- ``configure_logging()`` installs one console handler on stderr. The
  stage-by-stage lines you saw in rung 1 came from it, and the level you pass
  sets how much you see: ``"DEBUG"`` shows every command a run executes.
- A **hook** is a plain function the run calls at a named point.
  ``@flow.on_agent_output`` gets every line the agent prints, already parsed
  by its provider into events: here, the agent's text and each tool it
  calls, streamed as they happen. The others are ``on_run_start``,
  ``on_workspace_ready``, ``on_sandbox_ready``, ``on_agent_end``,
  ``on_integrated`` and ``on_run_end``.
- A **hook bundle** is an object with some of those methods. ``EventLog`` is
  one waystation ships: one JSON object per lifecycle event, written to a
  file you can read back, ``jq`` over, or load into a notebook. The built-in
  bundles use the same hooks yours do; there is no private channel.

Every hook gets a ``RunContext``. ``ctx.log`` is a logger already tagged with
the run's id, so a hook's own lines sit beside waystation's in the console
and carry the same ``[run-id]`` prefix. A hook that raises fails the run, as
``HookRaised``, so keep hooks small.

**This run lands on a branch, not on your HEAD.** ``Integration(BRANCH)``
moves only ``agents/observability``, and your checkout stays exactly where it
is. ``git log agents/observability`` shows what landed; delete the branch when
you are done with it.

**How to run it.** With the image built and a credential exported (see
examples/README.md), from inside the repo you want the agent to read::

    cd path/to/your/repo
    uv run --project path/to/waystation --group examples \\
        python path/to/waystation/examples/observability.py

Add ``--env-file path/to/.env`` to that ``uv run`` to load the credential from
a gitignored ``.env`` instead.

**What to watch.** Between waystation's stage lines, the agent narrates: a
``said:`` line for each thing it writes and a ``tool:`` line for each tool it
reaches for, tagged with the run id. When it ends, ``logs/events.jsonl`` in
the repo holds one line per lifecycle event; the ``agent_end`` line carries
the exit code, and the ``integrated`` line what landed. The file is appended
to, so a second run adds its lines below the first.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from pydantic import BaseModel

from waystation import (
    ClaudeCode,
    DockerSandbox,
    EventLog,
    Flow,
    Integration,
    PreflightError,
    RunConflicted,
    RunContext,
    RunFailed,
    RunSucceeded,
    Timeouts,
    configure_logging,
    handle_signals,
)
from waystation.agents import AgentLine, AgentText, AgentToolUse

BRANCH = "agents/observability"

PROMPT = """\
Read this repository and write ARCHITECTURE.md at its root: a short overview
of its modules and how they fit together. Change no other file. Commit
ARCHITECTURE.md with a one-line message.
"""


class Notes(BaseModel):
    """The Outcome the agent reports back."""

    summary: str
    files_changed: list[str]


async def main() -> int:
    configure_logging("INFO")
    handle_signals()

    # A bundle is any object with some of the on_<hook> methods. Opened with
    # `with`, the file is closed when the flow is done with it.
    with EventLog(Path("logs") / "events.jsonl") as events:
        flow = Flow(
            Path.cwd(),
            agent=ClaudeCode(model="sonnet", max_budget_usd=1.00),
            sandbox=DockerSandbox("waystation-dev"),
            integration=Integration(BRANCH),
            timeouts=Timeouts(
                workspace=60,
                sandbox=120,
                agent_silence=5 * 60,
                agent_wall=20 * 60,
                collect=60,
                integrate=60,
                teardown=60,
            ),
            hooks=[events],
        )

        # Registered on the flow, so every run the flow makes from here on
        # gets it. Each line arrives with the events its provider parsed out
        # of it: a stderr line, or a line the provider couldn't read, has
        # none.
        @flow.on_agent_output
        def narrate(ctx: RunContext, line: AgentLine) -> None:
            for event in line.events:
                match event:
                    case AgentText(text=text):
                        ctx.log.info("said: %s", text.strip()[:200])
                    case AgentToolUse(name=name):
                        ctx.log.info("tool: %s", name)

        @flow.on_run_end
        def where_the_log_is(ctx: RunContext, result: object) -> None:
            ctx.log.info("lifecycle events are in %s", events.path)

        try:
            result = await flow.run(PROMPT, outcome=Notes)
        except PreflightError as err:
            print(f"preflight: {err}")
            return 1

    match result:
        case RunSucceeded(outcome=notes):
            print(f"succeeded: {notes.summary}")
            print(f"  see it with: git log {BRANCH}")
            return 0
        case RunConflicted(preserved=preserved):
            print(f"conflicted: nothing landed; kept on {preserved}")
            return 1
        case RunFailed(stage=stage, failure=failure, preserved=preserved):
            print(f"failed at {stage}: {failure!r}")
            if preserved is not None:
                print(f"  the agent's commits are kept on {preserved}")
            return 1


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except asyncio.CancelledError:
        # handle_signals() cancelled the run, which has already cleaned up.
        sys.exit(130)
