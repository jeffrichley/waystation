"""Agent provider protocol, command, and Outcome events."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


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


AgentEvent = OutcomeReported | AgentText | AgentToolUse


@runtime_checkable
class AgentProvider(Protocol):
    def preflight(self) -> None: ...

    def command(self, prompt: str, outcome_schema: dict[str, Any]) -> AgentCommand: ...

    def parse(self, line: str) -> Sequence[AgentEvent]: ...
