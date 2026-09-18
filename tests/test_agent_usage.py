"""A run reports the token usage its agent reported, on the agent's exit (#36)."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import pytest

from helpers import OK_OUTCOME_LINE, USAGE, ShellAgent
from waystation import AgentExited, Flow, NoSandbox, RunFailed, RunSucceeded
from waystation.agents import AgentUsage

FIRST = AgentUsage(input_tokens=10, output_tokens=2, cost_usd=0.01, turns=1)
LAST = AgentUsage(
    input_tokens=30,
    output_tokens=7,
    cache_read_tokens=12,
    cache_write_tokens=8,
    cost_usd=0.04,
    turns=3,
)


def _says(usage: AgentUsage) -> str:
    """A shell line that prints ``usage`` the way ``ShellAgent`` parses it."""
    return f"echo '{USAGE}{json.dumps(asdict(usage))}'"


@pytest.mark.git
async def test_the_last_usage_the_agent_reports_lands_on_its_exit(
    host_repo: Path,
) -> None:
    script = "\n".join([_says(FIRST), _says(LAST), f"echo '{OK_OUTCOME_LINE}'"])

    result = await Flow(host_repo, agent=ShellAgent(script), sandbox=NoSandbox()).run(
        "p"
    )

    assert isinstance(result, RunSucceeded)
    assert result.agent is not None
    assert result.agent.usage == LAST


@pytest.mark.git
async def test_an_agent_that_exits_non_zero_keeps_the_usage_it_reported(
    host_repo: Path,
) -> None:
    script = "\n".join([_says(LAST), "exit 1"])

    result = await Flow(host_repo, agent=ShellAgent(script), sandbox=NoSandbox()).run(
        "p"
    )

    assert isinstance(result, RunFailed)
    assert isinstance(result.failure, AgentExited)
    assert result.agent is not None
    assert result.agent.usage == LAST
