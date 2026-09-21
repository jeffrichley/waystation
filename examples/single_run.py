"""Rung 1: one real agent, in Docker, landing on your own HEAD.

**What it teaches.** A flow script is a plain Python file you own. It builds a
``Flow`` (the host repo, which agent, which sandbox), asks it for a run,
awaits the run, and gets back one of exactly three values:

- ``RunSucceeded``: the agent finished, reported an Outcome that validated,
  and its commits landed;
- ``RunConflicted``: the Outcome validated, but the commits would not land
  cleanly. Nothing was touched, and ``preserved`` names the branch they are
  kept on;
- ``RunFailed``: some stage failed. ``stage`` says which, and ``failure`` says
  how, as a value you can ``match`` on too.

An awaited run never raises for a failure. It hands you the failure as a
value, so the ``match`` below is the whole of the error handling. Preflight is
the one exception: it proves Docker, the image and a credential are all
present before anything starts, and raises ``PreflightError`` rather than
start a run it knows will fail.

The agent is real Claude Code, running inside a fresh container from the
``waystation-dev`` image, so it can edit files and run commands without ever
touching your machine. The container sees a clone of your committed work,
never your uncommitted changes, and it is removed when the run ends.

**Your credential goes in by name, never by value.** ``ClaudeCode`` passes
``ANTHROPIC_API_KEY`` or ``CLAUDE_CODE_OAUTH_TOKEN`` through from your shell.
The container's environment is otherwise cleared, so nothing else of yours
reaches it. Name more with ``pass_env=`` if the agent needs them. Logs and
command lines elide what a credential holds.

**Loud note: this run lands on the HEAD of the repo you run it from.**
``.integrate("HEAD")`` fast-forwards your own checkout onto the agent's
commits:

- It refuses with ``Refused("dirty_tree")`` if any tracked file has
  uncommitted changes, or if any file of yours, ignored ones included, sits
  where the landing would write one. Your uncommitted work is never
  overwritten, and the agent's commits are kept on a preservation branch
  instead.
- The flow's own untracked output, such as a ``logs/`` directory inside the
  repo, doesn't block it.
- The landing rewrites whatever the agent changed, **this script included**,
  if you have copied it into the repo it targets and the agent edits it. The
  running process keeps the version it loaded; the next run reads the new
  one.

To land somewhere safer, pass a branch name instead: ``.integrate("agents/try")``
moves only that branch, and your checkout stays exactly where it is.

**How to run it.** Build the image once, export a credential, then run the
script from inside the repo you want the agent to work on::

    just example-image
    export ANTHROPIC_API_KEY=...   # or CLAUDE_CODE_OAUTH_TOKEN
    cd path/to/your/repo
    uv run --project path/to/waystation --group examples \\
        python path/to/waystation/examples/single_run.py

**What to watch.** The log lines on stderr walk the run's stages: workspace,
sandbox, agent, collect, integrate. The agent stage takes a minute or two.
When it ends, stdout says which of the three results came back. On success,
``git log -1`` shows the agent's commit on your branch and ``AGENT_NOTES.md``
is in your checkout. Press Ctrl-C at any point: the run tears its container
down and keeps whatever the agent had committed on a preservation branch.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from pydantic import BaseModel

from waystation import (
    ClaudeCode,
    DockerSandbox,
    Flow,
    PreflightError,
    RunConflicted,
    RunFailed,
    RunSucceeded,
    Timeouts,
    configure_logging,
    handle_signals,
)

# A situational prompt, so an inline string. Instructions you reuse across
# runs belong in a file, passed as a Path (see examples/README.md).
PROMPT = """\
Read this repository and write AGENT_NOTES.md at its root: five short bullets
saying what the project does and how it is laid out. Change no other file.
Commit AGENT_NOTES.md with a one-line message.
"""


class Notes(BaseModel):
    """The Outcome: what the agent reports back, validated before you see it."""

    summary: str
    files_changed: list[str]


async def main() -> int:
    # Logs go to stderr; stdout stays yours for the result below.
    configure_logging("INFO")
    # Ctrl-C or SIGTERM cancels this task, and the run cleans up after itself.
    handle_signals()

    flow = Flow(
        Path.cwd(),
        # The model and budget are yours to pick. ClaudeCode leaves both to
        # the CLI's own settings when you don't.
        agent=ClaudeCode(model="sonnet", max_budget_usd=1.00),
        # Waystation never builds or pulls an image: `just example-image`
        # builds this one.
        sandbox=DockerSandbox("waystation-dev"),
        # Every bound defaults to unbounded, so choose your own. These give
        # a small job plenty of room and a stuck one a way out: the agent is
        # stopped after five silent minutes, or twenty in all.
        timeouts=Timeouts(
            workspace=60,
            sandbox=120,
            agent_silence=5 * 60,
            agent_wall=20 * 60,
            collect=60,
            integrate=60,
            teardown=60,
        ),
    )

    try:
        result = await flow.run(PROMPT, outcome=Notes).integrate("HEAD")
    except PreflightError as err:
        print(f"preflight: {err}")
        return 1

    match result:
        case RunSucceeded(outcome=notes, report=report):
            print(f"succeeded: {notes.summary}")
            print(f"  files changed: {', '.join(notes.files_changed) or 'none'}")
            if report is not None:
                print(f"  landed {len(report.landed)} commit(s) on {report.target}")
            return 0
        case RunConflicted(preserved=preserved, report=report):
            paths = report.conflict.paths if report.conflict else ()
            print(f"conflicted on {', '.join(paths)}: nothing landed")
            print(f"  the agent's commits are kept on {preserved}")
            return 1
        case RunFailed(stage=stage, failure=failure, preserved=preserved):
            print(f"failed at {stage}: {failure!r}")
            if preserved is not None:
                print(f"  the agent's commits are kept on {preserved}")
            return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
