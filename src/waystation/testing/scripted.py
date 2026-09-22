"""ScriptedAgent: token-free provider that plays back canned output via sh."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, TypeAdapter

from waystation.agents.outcome import OUTCOME_MARKER, find_outcome
from waystation.agents.protocol import AgentCommand, AgentEvent, AgentText

__all__ = ["ScriptedAgent", "ScriptedCommit"]


def _shell_single_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _outcome_payload(outcome: Any) -> str:
    if outcome is None:
        return ""
    if isinstance(outcome, str):
        return outcome
    if isinstance(outcome, BaseModel):
        return json.dumps(outcome.model_dump(mode="json"))
    if isinstance(outcome, Mapping):
        return json.dumps(dict(outcome))
    return json.dumps(TypeAdapter(type(outcome)).dump_python(outcome, mode="json"))


def _write_file_commands(path: str, content: str) -> list[str]:
    parent = Path(path).parent.as_posix()
    cmds: list[str] = []
    if parent not in ("", "."):
        cmds.append(f"mkdir -p {_shell_single_quote(parent)}")
    cmds.append(
        f"printf '%s' {_shell_single_quote(content)} > {_shell_single_quote(path)}"
    )
    return cmds


@dataclass(frozen=True, slots=True)
class ScriptedCommit:
    """One commit the scripted agent creates before exiting.

    Attributes:
        message: The commit message.
        files: Each file's path, relative to the workspace, and the whole
            content it is written with.
    """

    message: str
    files: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class ScriptedAgent:
    """Play back canned lines, commits, and an Outcome through a shell script.

    Which shell is the sandbox's to say, so this plays back the same on
    ``NoSandbox`` and in a container with no argument either way (ADR-0036).

    Attributes:
        lines: Printed to stdout first, one per line.
        outcome: Reported on an Outcome marker line after the commits: a
            string as it is, anything else as JSON. ``None`` reports none.
        exit_code: What the script exits with.
        env: Values set in the agent's environment.
        pass_env: Host variable names passed through to the agent.
        commits: Made in order, after ``lines``.
        uncommitted: Files written after the commits and left uncommitted,
            for collect's salvage to find.
        delay: Seconds slept before anything else; ``None`` sleeps not at all.
        linger: Keep running after the Outcome, with children, until killed
            — for a test of completion grace or of cancellation.
        linger_touch: While lingering, a path a background child keeps
            appending to, so a test can see the tree is still alive.
    """

    lines: Sequence[str] = ()
    outcome: Any = None
    exit_code: int = 0
    env: Mapping[str, str] = field(default_factory=dict)
    pass_env: Sequence[str] = ()
    commits: Sequence[ScriptedCommit] = ()
    uncommitted: Mapping[str, str] = field(default_factory=dict)
    delay: float | None = None
    linger: bool = False
    linger_touch: str | None = None

    def preflight(self) -> None:
        """Nothing: the shell this needs is the sandbox's, and it checks it."""
        return None

    def command(self, prompt: str, outcome_schema: dict[str, Any]) -> AgentCommand:
        """A shell script that plays this agent back in the sandbox.

        It prints ``lines``, makes each of ``commits`` and leaves
        ``uncommitted`` written, reports ``outcome`` on a marker line, then
        exits ``exit_code`` — or lingers first, when asked to.

        Args:
            prompt: Ignored: playback says what it was scripted to.
            outcome_schema: Ignored, for the same reason.

        Returns:
            A ``script`` command, for the sandbox's own shell (ADR-0036).
        """
        del prompt, outcome_schema  # scripted playback ignores prompt/schema
        parts: list[str] = ["set -e"]
        if self.delay is not None:
            parts.append(f"sleep {float(self.delay)}")
        for line in self.lines:
            parts.append(f"printf '%s\\n' {_shell_single_quote(line)}")
        for commit in self.commits:
            for path, content in commit.files.items():
                parts.extend(_write_file_commands(path, content))
            parts.append("git add -A")
            parts.append(f"git commit -m {_shell_single_quote(commit.message)}")
        for path, content in self.uncommitted.items():
            parts.extend(_write_file_commands(path, content))
        payload = _outcome_payload(self.outcome)
        if payload:
            marker_line = f"{OUTCOME_MARKER} {payload}"
            parts.append(f"printf '%s\\n' {_shell_single_quote(marker_line)}")
        if self.linger:
            touch = self.linger_touch
            if touch is not None:
                q = _shell_single_quote(touch)
                parts.append(f"( while true; do printf x >> {q}; sleep 0.05; done ) &")
            parts.append("( sleep 999 ) &")
            # Keep the shell alive as process-group leader so cancel can kill
            # the whole tree (POSIX killpg / Windows Job Object + taskkill /T).
            parts.append("wait")
        parts.append(f"exit {int(self.exit_code)}")
        script = "\n".join(parts)
        return AgentCommand(
            script=script,
            stdin=None,
            env=self.env,
            pass_env=self.pass_env,
        )

    def parse(self, line: str) -> Sequence[AgentEvent]:
        """A marker line as its Outcome, any other line as text.

        Args:
            line: One line of the script's stdout.

        Returns:
            ``OutcomeReported`` for an Outcome marker line, ``AgentText`` for
            any other non-empty line, and nothing for an empty one.
        """
        # The public helper, so playback reads a marker exactly as a
        # provider author's agent would be read (#43).
        reported = find_outcome(line)
        if reported is not None:
            return [reported]
        return [AgentText(text=line)] if line else []
