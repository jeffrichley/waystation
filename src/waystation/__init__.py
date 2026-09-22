"""waystation — orchestrate sandboxed AI coding agents against git repos.

What a flow script writes and reads is here: build a ``Flow``, describe runs,
await them, match on results, register hooks, compose the primitives by hand,
turn on observability. The three seam protocols come with it, because
``Flow(agent=..., sandbox=...)`` and ``.integrate(...)`` are typed with them.

What you need only in order to *implement* a seam lives in that seam's
module, and there alone:

- ``waystation.agents`` — ``AgentCommand``, the event types a provider emits,
  and the marker-line helpers for an agent with no schema output of its own;
- ``waystation.sandbox`` — ``Sandbox``, ``ExecResult``, ``clone_in``,
  ``allowlisted_env``, and the host process runner and its strategies;
- ``waystation.integration`` — ``GitRepo``, ``GitResult``, ``Target`` and the
  landing values a strategy builds a report from;
- ``waystation.testing`` — ``ScriptedAgent`` and the conformance suite
  (ADR-0044).

A seam module also re-exports what a flow script hands it — ``DockerSandbox``
is in both, because a script writes it and a backend author reads it as the
worked example. What a flow script never writes does not come up here,
however public it is: a second spelling of a seam's own name leaves every
reader deciding which one is real (#174).
"""

from __future__ import annotations

from waystation.agents import (
    AgentProvider,
    run_agent,
)
from waystation.agents.claude_code import ClaudeCode
from waystation.collect import PatchSeries, collect
from waystation.errors import PreflightError, StageError, WaystationError
from waystation.fan_out import fan_out
from waystation.flow import Flow, RunSpec
from waystation.hooks import HookBundle, RunContext
from waystation.integration import (
    Integration,
    IntegrationStrategy,
    Squash,
    integrate,
    preserve_series,
)
from waystation.observability import configure_logging, run_logger
from waystation.observers import Dashboard, EventLog, RunLogFiles
from waystation.preflight import preflight
from waystation.queue import queue
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
    NoSandbox,
    SandboxBackend,
)
from waystation.signals import handle_signals
from waystation.stages import stages
from waystation.workspace import Workspace, prepare_workspace, remove_workspace

__all__ = [
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
    "Failure",
    "Flow",
    "HookBundle",
    "HookName",
    "HookRaised",
    "Integration",
    "IntegrationReport",
    "IntegrationStrategy",
    "NoSandbox",
    "OutcomeInvalid",
    "OutcomeMissing",
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
    "SandboxBackend",
    "Series",
    "Squash",
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
    "handle_signals",
    "integrate",
    "preserve_series",
    "preflight",
    "prepare_workspace",
    "queue",
    "remove_workspace",
    "run_agent",
    "run_logger",
    "stages",
]
