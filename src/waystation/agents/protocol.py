"""Agent provider protocol, command, and Outcome events."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from waystation.results import AgentUsage


@dataclass(frozen=True, slots=True)
class AgentCommand:
    argv: Sequence[str]
    stdin: str | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    pass_env: Sequence[str] = ()


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
