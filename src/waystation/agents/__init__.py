"""Public agent package surface."""

from __future__ import annotations

from waystation.agents.outcome import find_outcome, outcome_instructions
from waystation.agents.protocol import (
    AgentCommand,
    AgentEvent,
    AgentLine,
    AgentProvider,
    AgentText,
    AgentToolUse,
    OutcomeReported,
)
from waystation.agents.run_agent import run_agent
from waystation.agents.scripted import ScriptedAgent, ScriptedCommit
from waystation.results import AgentUsage

__all__ = [
    "AgentCommand",
    "AgentEvent",
    "AgentLine",
    "AgentProvider",
    "AgentText",
    "AgentToolUse",
    "AgentUsage",
    "OutcomeReported",
    "ScriptedAgent",
    "ScriptedCommit",
    "find_outcome",
    "outcome_instructions",
    "run_agent",
]
