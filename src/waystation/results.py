"""Result and configuration value types."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import ValidationError

from waystation._redaction import redact_argv

__all__ = [
    "AgentExit",
    "AgentExited",
    "AgentUsage",
    "CommandFailed",
    "Conflict",
    "Errored",
    "FailedPatch",
    "Failure",
    "HookName",
    "HookRaised",
    "IntegrationReport",
    "OutcomeInvalid",
    "OutcomeMissing",
    "RefusalReason",
    "Refused",
    "RunConflicted",
    "RunFailed",
    "RunResult",
    "RunSucceeded",
    "Series",
    "Stage",
    "Summary",
    "TimedOut",
    "Timeouts",
]

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
    """What the agent reported spending: tokens, and cost and turns when it says.

    Fields a provider does not report are ``None``.
    """

    input_tokens: int
    output_tokens: int
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    cost_usd: float | None = None
    turns: int | None = None


@dataclass(frozen=True, slots=True)
class AgentExit:
    """How the agent's exec ended.

    ``cancelled`` says the run was cancelled while the agent worked, and its
    tree was killed (ADR-0023): there is no exit code, so ``exit_code`` is -1.
    """

    exit_code: int
    elapsed: float
    hanging: bool
    usage: AgentUsage | None = None
    cancelled: bool = False


@dataclass(frozen=True, slots=True)
class Series:
    """The patch series a run collected, as its result reports it.

    ``salvaged`` says its last commit is work collect committed for the agent.
    """

    commits: int
    salvaged: bool


@dataclass(frozen=True, slots=True)
class FailedPatch:
    """The patch an ``apply`` replay stopped at.

    ``index`` is its place in the series, counting from 0, so
    ``series.patches[index]`` is the patch itself.
    """

    index: int
    subject: str


@dataclass(frozen=True, slots=True)
class Conflict:
    """Why a landing stopped: the paths that conflicted.

    ``failed_patch`` is set for ``apply``, which replays one patch at a time;
    ``merge`` and ``Squash`` land the series in one step, so there is no
    patch to name.
    """

    paths: tuple[str, ...]
    failed_patch: FailedPatch | None = None


@dataclass(frozen=True, slots=True)
class IntegrationReport:
    """What integration did for a run.

    On a conflict ``conflict`` is set, ``target_after`` is ``None`` and
    ``landed`` is empty: the target never moved.
    """

    strategy: str
    target: str
    mechanism: str
    target_before: str
    target_after: str | None
    landed: tuple[str, ...]
    conflict: Conflict | None = None


@dataclass(frozen=True, slots=True)
class TimedOut:
    """A bound ran out: ``bound`` names it, after ``elapsed`` of ``limit`` seconds."""

    bound: str
    limit: float
    elapsed: float


@dataclass(frozen=True, slots=True)
class AgentExited:
    """The agent exited non-zero, with the tails of what it wrote.

    ``outcome`` is the Outcome it reported before failing, if it did.
    """

    exit_code: int
    stdout_tail: str
    stderr_tail: str
    outcome: Any | None = None


@dataclass(frozen=True, slots=True)
class OutcomeMissing:
    """The agent exited without reporting an Outcome."""

    stdout_tail: str


@dataclass(frozen=True, slots=True)
class OutcomeInvalid:
    """The agent reported an Outcome, ``raw``, that failed validation."""

    raw: Any
    error: ValidationError


@dataclass(frozen=True, slots=True)
class HookRaised:
    """The hook ``function``, registered for ``hook``, raised ``exception``."""

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
    """A stage declined to go on, for ``reason``; ``detail`` says more."""

    reason: RefusalReason
    detail: str


@dataclass(frozen=True, slots=True)
class Errored:
    """A stage raised an exception no other kind of failure describes."""

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
class RunConflicted[OutcomeT]:
    """A run whose Outcome validated but whose landing conflicted.

    Nothing landed: ``report.conflict`` says where it stopped, and
    ``preserved`` names the branch the series is kept on, ready for a
    resolver run (ADR-0015).
    """

    run_id: str
    name: str | None
    base_sha: str | None
    elapsed: Mapping[Stage, float]
    agent: AgentExit | None
    series: Series | None
    preserved: str | None
    outcome: OutcomeT
    report: IntegrationReport


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


type RunResult[OutcomeT] = RunSucceeded[OutcomeT] | RunConflicted[OutcomeT] | RunFailed
"""What awaiting a run returns; ``match`` on the three kinds, as with ``Failure``."""
