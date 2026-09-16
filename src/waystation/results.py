"""Result and configuration value types."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

Stage = Literal["workspace", "sandbox", "agent", "collect", "integrate"]


@dataclass(frozen=True, slots=True)
class Summary:
    """Default Outcome when a run does not declare one."""

    summary: str


@dataclass(frozen=True, slots=True)
class Timeouts:
    """Per-stage bounds in seconds; ``None`` means unbounded."""

    workspace: float | None = None
    sandbox: float | None = None
    agent_silence: float | None = None
    agent_wall: float | None = None
    collect: float | None = None
    integrate: float | None = None
    teardown: float | None = None
    completion_grace: float = 30.0


@dataclass(frozen=True, slots=True)
class AgentUsage:
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    cost_usd: float | None = None
    turns: int | None = None


@dataclass(frozen=True, slots=True)
class AgentExit:
    exit_code: int
    elapsed: float
    hanging: bool
    usage: AgentUsage | None = None


@dataclass(frozen=True, slots=True)
class Series:
    commits: int
    salvaged: bool


@dataclass(frozen=True, slots=True)
class RunSucceeded[OutcomeT]:
    """A run that completed its agent stage with a validated Outcome."""

    run_id: str
    name: str | None
    base_sha: str | None
    elapsed: Mapping[Stage, float]
    agent: AgentExit | None
    series: Series | None
    preserved: str | None
    outcome: OutcomeT
    report: None = None
