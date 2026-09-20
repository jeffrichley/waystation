"""One resolver builds a run's environment, and each tier reaches its own execs.

The table ADR-0034 settles, for the two tiers that exist today (#29 adds the
per-run one):

| Tier                     | Sandbox start | Agent exec | Other execs |
| ------------------------ | ------------- | ---------- | ----------- |
| Sandbox spec             | yes           | yes        | yes         |
| Agent provider           | no            | yes        | no          |
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from helpers import OK_OUTCOME_LINE, ShellAgent, sh
from waystation import (
    Flow,
    NoSandbox,
    RunContext,
    RunSucceeded,
    Summary,
)
from waystation.agents import AgentLine
from waystation.sandbox import allowlisted_env

SPEC = "WAYSTATION_SPEC"
AGENT = "WAYSTATION_AGENT"
HOST = "WAYSTATION_HOST"
BOTH = "WAYSTATION_BOTH"
MOVES = "WAYSTATION_MOVES"


def _as_env(text: str) -> dict[str, str]:
    pairs = (line.split("=", 1) for line in text.splitlines() if "=" in line)
    return {key: value for key, value in pairs if key}


@pytest.mark.unit
def test_a_literal_beats_a_pass_through_name() -> None:
    """Naming one key both ways resolves to the value the user typed."""
    built = allowlisted_env(
        literal={BOTH: "literal"},
        pass_env=(BOTH,),
        host={BOTH: "from-host"},
    )

    assert built == {BOTH: "literal"}


@pytest.mark.unit
def test_a_pass_through_name_the_host_lacks_is_skipped_by_name(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``pass_env=("CI",)`` has to stay usable on a laptop (ADR-0013)."""
    with caplog.at_level(logging.DEBUG, logger="waystation"):
        built = allowlisted_env(
            literal={}, pass_env=(HOST, "WAYSTATION_ABSENT"), host={HOST: "here"}
        )

    assert built == {HOST: "here"}
    said = " ".join(r.getMessage() for r in caplog.records)
    assert "WAYSTATION_ABSENT" in said, "the skipped name belongs in the log"
    assert "here" not in said, "a value never does (ADR-0025)"


@pytest.mark.unit
def test_base_keys_come_from_the_host_and_lose_to_a_literal() -> None:
    built = allowlisted_env(
        literal={"PATH": "mine"},
        pass_env=(),
        base=("PATH", "HOME"),
        host={"PATH": "theirs", "HOME": "/home/x"},
    )

    assert built == {"PATH": "mine", "HOME": "/home/x"}


def _env_printing_agent(**kwargs: object) -> ShellAgent:
    """An agent that prints its own environment, then reports."""
    return ShellAgent(f"env\necho '{OK_OUTCOME_LINE}'", **kwargs)  # type: ignore[arg-type]


@pytest.mark.git
async def test_a_providers_environment_reaches_its_agent_and_nothing_else(
    host_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A provider's credential must not reach collect or ``clone_in``."""
    monkeypatch.setenv(HOST, "from-host")
    agent_lines: list[str] = []
    other: list[str] = []

    async def keep(ctx: RunContext, line: AgentLine) -> None:
        agent_lines.append(line.raw)

    async def env_outside(ctx: RunContext) -> None:
        other.append((await ctx.sandbox.exec([sh(), "-c", "env"])).stdout)

    flow = Flow(
        host_repo,
        agent=_env_printing_agent(env={AGENT: "agent"}, pass_env=(HOST,)),
        sandbox=NoSandbox(env={SPEC: "spec"}),
    )

    result = await (
        flow.run("print the environment", outcome=Summary)
        .on_agent_output(keep)
        .on_sandbox_ready(env_outside)
    )

    assert isinstance(result, RunSucceeded), result
    inside = _as_env("\n".join(agent_lines))
    outside = _as_env(other[0])

    # The spec's tier reaches every exec.
    assert inside[SPEC] == "spec"
    assert outside[SPEC] == "spec"
    # The provider's reaches its agent, and stops there.
    assert inside[AGENT] == "agent"
    assert inside[HOST] == "from-host"
    assert AGENT not in outside
    assert HOST not in outside


@pytest.mark.git
async def test_a_literal_beats_a_pass_through_name_for_an_agent_too(
    host_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``run_agent`` used to resolve this the other way round from the backends."""
    monkeypatch.setenv(BOTH, "from-host")
    agent_lines: list[str] = []

    async def keep(ctx: RunContext, line: AgentLine) -> None:
        agent_lines.append(line.raw)

    flow = Flow(
        host_repo,
        agent=_env_printing_agent(env={BOTH: "literal"}, pass_env=(BOTH,)),
        sandbox=NoSandbox(),
    )

    result = await flow.run("print it", outcome=Summary).on_agent_output(keep)

    assert isinstance(result, RunSucceeded), result
    assert _as_env("\n".join(agent_lines))[BOTH] == "literal"


@pytest.mark.git
async def test_one_run_sees_one_host_environment(
    host_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The host is read once, so a run never straddles two environments."""
    monkeypatch.setenv(MOVES, "before")
    agent_lines: list[str] = []

    async def keep(ctx: RunContext, line: AgentLine) -> None:
        agent_lines.append(line.raw)

    def move_it(ctx: RunContext) -> None:
        os.environ[MOVES] = "after"

    flow = Flow(
        host_repo,
        agent=_env_printing_agent(pass_env=(MOVES,)),
        sandbox=NoSandbox(pass_env=(MOVES,)),
    )

    result = await (
        flow.run("print it", outcome=Summary)
        .on_agent_output(keep)
        .on_sandbox_ready(move_it)
    )

    assert isinstance(result, RunSucceeded), result
    assert _as_env("\n".join(agent_lines))[MOVES] == "before"
