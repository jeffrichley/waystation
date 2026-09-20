"""waystation — orchestrate sandboxed AI coding agents against git repos."""

from __future__ import annotations

from waystation.agents import (
    AgentCommand,
    AgentProvider,
    OutcomeReported,
    ScriptedAgent,
    ScriptedCommit,
    find_outcome,
    outcome_instructions,
    run_agent,
)
from waystation.agents.claude_code import ClaudeCode
from waystation.collect import PatchSeries, collect
from waystation.errors import PreflightError, StageError, WaystationError
from waystation.fan_out import fan_out
from waystation.flow import Flow, RunSpec
from waystation.hooks import HookBundle, RunContext
from waystation.integration import (
    GitRepo,
    GitResult,
    Integration,
    IntegrationStrategy,
    integrate,
)
from waystation.observability import configure_logging
from waystation.observers import Dashboard, EventLog, RunLogFiles
from waystation.results import (
    AgentExit,
    AgentExited,
    AgentUsage,
    CommandFailed,
    Errored,
    Failure,
    HookName,
    HookRaised,
    IntegrationReport,
    OutcomeInvalid,
    OutcomeMissing,
    Refused,
    RunConflicted,
    RunFailed,
    RunResult,
    RunSucceeded,
    Series,
    Stage,
    Summary,
    TimedOut,
    Timeouts,
)
from waystation.sandbox import (
    DockerSandbox,
    ExecResult,
    NoSandbox,
    Sandbox,
    SandboxBackend,
)
from waystation.signals import handle_signals
from waystation.workspace import Workspace, prepare_workspace

__all__ = [
    "AgentCommand",
    "AgentExit",
    "AgentExited",
    "AgentProvider",
    "AgentUsage",
    "ClaudeCode",
    "CommandFailed",
    "Dashboard",
    "DockerSandbox",
    "Errored",
    "EventLog",
    "ExecResult",
    "Failure",
    "Flow",
    "GitRepo",
    "GitResult",
    "HookBundle",
    "HookName",
    "HookRaised",
    "Integration",
    "IntegrationReport",
    "IntegrationStrategy",
    "NoSandbox",
    "OutcomeInvalid",
    "OutcomeMissing",
    "OutcomeReported",
    "PatchSeries",
    "PreflightError",
    "Refused",
    "RunConflicted",
    "RunContext",
    "RunFailed",
    "RunLogFiles",
    "RunResult",
    "RunSpec",
    "RunSucceeded",
    "Sandbox",
    "SandboxBackend",
    "ScriptedAgent",
    "ScriptedCommit",
    "Series",
    "Stage",
    "StageError",
    "Summary",
    "TimedOut",
    "Timeouts",
    "WaystationError",
    "Workspace",
    "collect",
    "configure_logging",
    "fan_out",
    "find_outcome",
    "handle_signals",
    "integrate",
    "outcome_instructions",
    "prepare_workspace",
    "run_agent",
]
