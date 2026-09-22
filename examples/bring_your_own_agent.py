"""Rung 7: bring your own agent, here the Cursor agent CLI.

**What it teaches.** An agent provider is a small class you can write
yourself. ``ClaudeCode`` has no special path into waystation: it implements
the same three methods as the ``Cursor`` provider below, and any object with
them works as ``agent=``.

- ``preflight()`` fails fast, before any run starts, if the agent can't work:
  here, when no Cursor credential will reach it.
- ``command(prompt, outcome_schema)`` says what to run in the sandbox. It is
  pure: it builds an ``AgentCommand`` and runs nothing.
- ``parse(line)`` turns one line of the agent's stdout into events: text to
  show, tool calls, and the Outcome.

**The Outcome, for an agent with no schema output of its own.** Claude Code
takes the Outcome's JSON Schema as a flag and re-prompts until the answer
matches. Cursor has no such flag, so this provider uses the two helpers
waystation ships for that case (ADR-0019):

- ``outcome_instructions(schema)`` is appended to the prompt. It asks the
  agent to end with one line starting ``!?`` and holding the Outcome as JSON.
- ``find_outcome(text)`` finds that line in what the agent wrote, and returns
  the report to hand back from ``parse``, or ``None``. A marker whose JSON is
  broken is still reported, so a malformed answer fails the run as
  ``OutcomeInvalid``, saying what the agent wrote, rather than as
  ``OutcomeMissing``.

Waystation validates the report against your Outcome type either way. The
provider never does: it only says what the agent reported.

**When is the agent done?** Cursor's ``stream-json`` output ends with a
``result`` event, but the provider doesn't wait for it. The Outcome is looked
for in every assistant message as it arrives, and the run is judged the way
waystation judges every agent: by the exit code once stdout reaches EOF. A
CLI that exits 0 without a ``result`` line still succeeds if it reported an
Outcome on the way.

**The prompt rides on the command line.** ``cursor-agent`` takes its prompt
as an argument, so this provider binds it into ``argv`` after ``--``, which
keeps a prompt starting with a dash from being read as a flag. A command
line is more public than stdin: other processes on the host can see it while
the agent runs, and ``waystation.sandbox`` logs it at DEBUG. That's fine for a
prompt; a secret never belongs in one. (``ClaudeCode`` sends its prompt on
stdin instead.)

**Your credential goes in by name.** ``Cursor`` passes ``CURSOR_API_KEY``
through from the environment this script runs in, and preflight refuses to
start without it. Create a key in Cursor's dashboard, then export it, or put
it in the gitignored ``.env`` (see ``.env.example``) and load it with
``uv run --env-file .env``.

**How to run it.** Build the Cursor image once, then run the script from
inside a repo you want the agent to work on::

    just example-image cursor
    cd path/to/your/repo
    uv run --project path/to/waystation --group examples \\
        --env-file path/to/waystation/.env \\
        python path/to/waystation/examples/bring_your_own_agent.py

The agent's commits land on the branch ``agents/cursor``, never on your
checkout, so your working tree is untouched whatever it does.

**What to watch.** The console shows the stages as in rung 1. With
``configure_logging("DEBUG")`` it also shows every raw line Cursor prints,
which is the stream of JSON events ``parse`` reads. A ``Dashboard`` (rung 6)
shows what ``parse`` made of them: the text of each assistant message. When
the run ends, stdout says which result came back, and ``git log agents/cursor``
shows the agent's work.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from waystation import (
    DockerSandbox,
    Flow,
    Integration,
    PreflightError,
    RunConflicted,
    RunFailed,
    RunSucceeded,
    Timeouts,
    configure_logging,
    handle_signals,
)
from waystation.agents import (
    AgentCommand,
    AgentEvent,
    AgentText,
    AgentToolUse,
    find_outcome,
    outcome_instructions,
)

_CREDENTIAL = "CURSOR_API_KEY"


@dataclass(frozen=True, slots=True)
class Cursor:
    """The Cursor agent CLI as an agent provider.

    Frozen and stateless, like every provider: one instance serves every run,
    even concurrent ones in a fan-out.

    Attributes:
        model: ``--model``, e.g. ``"sonnet-4"``; ``None`` leaves it to
            Cursor's own default.
    """

    model: str | None = None

    def preflight(self) -> None:
        """Refuse to start without a credential that will reach the agent."""
        if not os.environ.get(_CREDENTIAL):
            msg = f"Cursor needs {_CREDENTIAL}: create one in Cursor's dashboard"
            raise PreflightError(msg)

    def command(self, prompt: str, outcome_schema: dict[str, Any]) -> AgentCommand:
        argv = [
            "cursor-agent",
            "--print",
            # One JSON event per line, so the silence timer resets as the
            # agent works; plain text arrives only at the end.
            "--output-format",
            "stream-json",
            # Unattended: nobody is there to approve a command or trust the
            # workspace. The sandbox is the boundary instead.
            "--force",
            "--trust",
        ]
        if self.model is not None:
            argv += ["--model", self.model]
        argv += ["--", f"{prompt}\n\n{outcome_instructions(outcome_schema)}"]
        return AgentCommand(argv=tuple(argv), pass_env=(_CREDENTIAL,))

    def parse(self, line: str) -> Sequence[AgentEvent]:
        # A raising parse would fail the run, so a line that isn't a JSON
        # event is skipped rather than trusted.
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return ()
        if not isinstance(event, dict):
            return ()
        kind = event.get("type")
        if kind == "assistant":
            return _assistant_events(event)
        if kind == "tool_call" and event.get("subtype") == "started":
            return _tool_call_events(event)
        if kind == "result" and not event.get("is_error"):
            # The whole final answer again. Reporting the same Outcome twice
            # is harmless: the last report wins.
            reported = find_outcome(str(event.get("result", "")))
            return [reported] if reported is not None else ()
        return ()


def _assistant_events(event: dict[str, Any]) -> list[AgentEvent]:
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    events: list[AgentEvent] = []
    for block in content if isinstance(content, list) else ():
        text = block.get("text") if isinstance(block, dict) else None
        if not isinstance(text, str):
            continue
        events.append(AgentText(text))
        reported = find_outcome(text)
        if reported is not None:
            events.append(reported)
    return events


def _tool_call_events(event: dict[str, Any]) -> list[AgentEvent]:
    # {"tool_call": {"readToolCall": {"args": {...}}}}: one key, the tool.
    call = event.get("tool_call")
    if not isinstance(call, dict) or len(call) != 1:
        return []
    (key, body), *_ = call.items()
    args = body.get("args") if isinstance(body, dict) else None
    name = key.removesuffix("ToolCall")
    return [AgentToolUse(name, args if isinstance(args, dict) else {})]


# A situational prompt, so an inline string (see examples/README.md).
PROMPT = """\
Find one function in this repository that has no docstring, and add a short
one saying what it does. Change nothing else, and commit your change with a
one-line message.
"""


class Change(BaseModel):
    """The Outcome: what the agent says it did."""

    summary: str
    files_changed: list[str]


async def main() -> int:
    configure_logging("INFO")
    handle_signals()

    flow = Flow(
        Path.cwd(),
        agent=Cursor(),
        # `just example-image cursor` builds it: the examples' image plus
        # the Cursor CLI.
        sandbox=DockerSandbox("waystation-cursor"),
        # A named branch: your checkout is never touched (rung 1 lands on
        # HEAD; this one doesn't need to).
        integration=Integration("agents/cursor"),
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
        result = await flow.run(PROMPT, outcome=Change)
    except PreflightError as err:
        print(f"preflight: {err}")
        return 1

    match result:
        case RunSucceeded(outcome=change, report=report):
            print(f"succeeded: {change.summary}")
            print(f"  files changed: {', '.join(change.files_changed) or 'none'}")
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
    try:
        sys.exit(asyncio.run(main()))
    except asyncio.CancelledError:
        # handle_signals() cancelled the run, which has already cleaned up.
        sys.exit(130)
