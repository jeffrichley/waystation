"""Result and configuration value types."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import ValidationError

from waystation._redaction import redact_argv

Stage = Literal["workspace", "sandbox", "agent", "collect", "integrate"]

HookName = Literal[
    "run_start",
    "workspace_ready",
    "sandbox_ready",
    "agent_output",
    "agent_end",
    "integrated",
    "run_end",
]

RefusalReason = Literal[
    "dirty_tree",
    "target_checked_out",
    "target_moved",
    "missing_extra_ref",
    "image_missing",
    "nonlinear_series",
    "no_git_identity",
]


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
class IntegrationReport:
    """What integration did for a run."""

    strategy: str
    target: str
    mechanism: str
    target_before: str
    target_after: str | None
    landed: tuple[str, ...]
    conflict: None = None


@dataclass(frozen=True, slots=True)
class TimedOut:
    bound: str
    limit: float
    elapsed: float


@dataclass(frozen=True, slots=True)
class AgentExited:
    exit_code: int
    stdout_tail: str
    stderr_tail: str
    outcome: Any | None = None


@dataclass(frozen=True, slots=True)
class OutcomeMissing:
    stdout_tail: str


@dataclass(frozen=True, slots=True)
class OutcomeInvalid:
    raw: Any
    error: ValidationError


@dataclass(frozen=True, slots=True)
class HookRaised:
    hook: HookName
    function: str
    exception: BaseException


@dataclass(frozen=True, slots=True)
class CommandFailed:
    """A command exited non-zero. ``argv`` is redacted, whoever built it (ADR-0025)."""

    argv: Sequence[str]
    exit_code: int
    stderr_tail: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "argv", redact_argv(self.argv))


@dataclass(frozen=True, slots=True)
class Refused:
    reason: RefusalReason
    detail: str


@dataclass(frozen=True, slots=True)
class Errored:
    exception: BaseException


Failure = (
    TimedOut
    | AgentExited
    | OutcomeMissing
    | OutcomeInvalid
    | HookRaised
    | CommandFailed
    | Refused
    | Errored
)


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
    report: IntegrationReport | None = None


@dataclass(frozen=True, slots=True)
class RunFailed:
    """A run that failed at a stage; never raised from an awaited run."""

    run_id: str
    name: str | None
    base_sha: str | None
    elapsed: Mapping[Stage, float]
    agent: AgentExit | None
    series: Series | None
    preserved: str | None
    stage: Stage
    failure: Failure
