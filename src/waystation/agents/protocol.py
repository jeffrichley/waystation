"""Agent provider protocol, command, and Outcome events."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from waystation.results import AgentUsage

__all__ = [
    "AgentCommand",
    "AgentEvent",
    "AgentLine",
    "AgentProvider",
    "AgentText",
    "AgentToolUse",
    "OutcomeReported",
]


@dataclass(frozen=True, slots=True)
class AgentCommand:
    """What to run for one agent: a literal ``argv``, or a shell ``script``.

    A provider that binds a CLI names ``argv``. One that plays back a script
    names ``script``, and the sandbox's own shell runs it — a provider never
    has to know whether its sandbox is this host or a container, which is a
    question it cannot answer anyway (ADR-0036).

    Exactly one of the two, because "run this argv" and "run this script in
    whatever shell you have" are different requests and neither implies the
    other.

    Attributes:
        argv: The command line to exec, run directly with no shell.
        stdin: Text fed to the agent's stdin — the prompt, for a CLI that
            reads it there.
        env: Values set in the agent's environment, over the sandbox's.
        pass_env: Host variable names passed through to the agent, where the
            host has them.
        script: A script the sandbox's own shell runs, instead of ``argv``.

    Raises:
        ValueError: When it names both ``argv`` and ``script``, or neither.
    """

    argv: Sequence[str] = ()
    stdin: str | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    pass_env: Sequence[str] = ()
    # Last, so that every positional construction that worked before still
    # binds the same way: this field is new, and `argv` is still first.
    script: str | None = None

    def __post_init__(self) -> None:
        if bool(self.argv) == (self.script is not None):
            named = "both" if self.argv else "neither"
            msg = (
                f"an AgentCommand runs an argv or a script, and this names "
                f"{named}: pass argv=(...) for a command line, or "
                f"script='...' to run one in the sandbox's own shell"
            )
            raise ValueError(msg)

    def argv_in(self, shell: Sequence[str]) -> Sequence[str]:
        """The argv to exec, given the shell of the sandbox it runs in.

        Args:
            shell: The sandbox's ``Sandbox.shell``, the prefix a script
                follows.

        Returns:
            ``argv`` as it is, or ``script`` behind ``shell``.
        """
        return self.argv if self.script is None else (*shell, self.script)


@dataclass(frozen=True, slots=True)
class OutcomeReported:
    """Provider-reported Outcome payload; core validates it."""

    raw: Any


@dataclass(frozen=True, slots=True)
class AgentText:
    """Text the agent said, as its provider parsed it from a line.

    Attributes:
        text: The text, whole.
    """

    text: str


@dataclass(frozen=True, slots=True)
class AgentToolUse:
    """A tool the agent called, as its provider parsed it from a line.

    Attributes:
        name: The tool's name.
        input: The arguments it was called with.
    """

    name: str
    input: Mapping[str, Any]


# AgentUsage is also the value AgentExit carries: the last one reported wins.
AgentEvent = OutcomeReported | AgentText | AgentToolUse | AgentUsage


@dataclass(frozen=True, slots=True)
class AgentLine:
    """One line the agent exec emitted, with the events its provider parsed.

    stderr lines are never parsed, so their ``events`` is always empty.
    """

    stream: Literal["stdout", "stderr"]
    raw: str
    events: tuple[AgentEvent, ...] = ()


@runtime_checkable
class AgentProvider(Protocol):
    """An agent CLI, told what to run and how to read what it prints.

    A provider is pure: it builds a command and parses lines, and core runs
    the command in the sandbox. ``ClaudeCode`` is one; ``ScriptedAgent`` in
    ``waystation.testing`` is another, for tests.
    """

    def preflight(self) -> None:
        """Check, before any run starts, that this agent can run.

        A credential the agent needs, say. Nothing here may look inside a
        sandbox: what one holds is known only once it starts (ADR-0018).

        Raises:
            PreflightError: When the agent cannot run, saying what is missing.
        """
        ...

    def command(self, prompt: str, outcome_schema: dict[str, Any]) -> AgentCommand:
        """The command that runs the agent on ``prompt``.

        Called when ``run_agent`` is, before anything execs. A provider that
        raises here fails the run with nothing started.

        Args:
            prompt: The run's prompt text.
            outcome_schema: The JSON schema of the Outcome the agent must
                report, derived from the run's Outcome type (ADR-0019).

        Returns:
            What to run: an ``argv`` for a CLI, or a ``script`` for the
            sandbox's own shell.
        """
        ...

    def parse(self, line: str) -> Sequence[AgentEvent]:
        """The events one line of the agent's stdout carries.

        A line it cannot read should yield none: a raise here cancels the
        agent and fails the run.

        Args:
            line: One stdout line, its line ending stripped.

        Returns:
            Its events, in order — an ``OutcomeReported`` for the Outcome,
            ``AgentUsage`` for what the run spent so far; empty for most.
        """
        ...
