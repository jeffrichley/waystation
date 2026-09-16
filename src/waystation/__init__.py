"""waystation — orchestrate sandboxed AI coding agents against git repos."""

from __future__ import annotations

from waystation.agents import (
    AgentCommand,
    AgentProvider,
    OutcomeReported,
    find_outcome,
    outcome_instructions,
    run_agent,
)
from waystation.errors import PreflightError, StageError, WaystationError
from waystation.flow import Flow, RunSpec
from waystation.results import (
    AgentExit,
    AgentExited,
    AgentUsage,
    CommandFailed,
    Errored,
    Failure,
    HookRaised,
    OutcomeInvalid,
    OutcomeMissing,
    Refused,
    RunFailed,
    RunSucceeded,
    Series,
    Stage,
    Summary,
    TimedOut,
    Timeouts,
)
from waystation.sandbox import ExecResult, NoSandbox, Sandbox, SandboxBackend
from waystation.workspace import Workspace, prepare_workspace

__all__ = [
    "AgentCommand",
    "AgentExit",
    "AgentExited",
    "AgentProvider",
    "AgentUsage",
    "CommandFailed",
    "Errored",
    "ExecResult",
    "Failure",
    "Flow",
    "HookRaised",
    "NoSandbox",
    "OutcomeInvalid",
    "OutcomeMissing",
    "OutcomeReported",
    "PreflightError",
    "Refused",
    "RunFailed",
    "RunSpec",
    "RunSucceeded",
    "Sandbox",
    "SandboxBackend",
    "Series",
    "Stage",
    "StageError",
    "Summary",
    "TimedOut",
    "Timeouts",
    "WaystationError",
    "Workspace",
    "find_outcome",
    "outcome_instructions",
    "prepare_workspace",
    "run_agent",
]
