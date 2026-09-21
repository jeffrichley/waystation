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
        """The argv to exec, given the shell of the sandbox it runs in."""
        return self.argv if self.script is None else (*shell, self.script)


@dataclass(frozen=True, slots=True)
class OutcomeReported:
    """Provider-reported Outcome payload; core validates it."""

    raw: Any


@dataclass(frozen=True, slots=True)
class AgentText:
    text: str


@dataclass(frozen=True, slots=True)
class AgentToolUse:
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
    def preflight(self) -> None: ...

    def command(self, prompt: str, outcome_schema: dict[str, Any]) -> AgentCommand: ...

    def parse(self, line: str) -> Sequence[AgentEvent]: ...
