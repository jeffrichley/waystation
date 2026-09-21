"""One resolver builds a run's environment, and each tier reaches its own execs.

The table ADR-0034 settles, and the order ADR-0013 ranks the tiers in —
spec, then provider, then per run, the most specific winning:

| Tier                     | Sandbox start | Agent exec | Other execs |
| ------------------------ | ------------- | ---------- | ----------- |
| Sandbox spec             | yes           | yes        | yes         |
| Agent provider           | no            | yes        | no          |
| Per run (``.env()``)     | yes           | yes        | yes         |
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from helpers import OK_OUTCOME_LINE, ShellAgent, git
from waystation import (
    Flow,
    NoSandbox,
    RunContext,
    RunSpec,
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
        host_env={BOTH: "from-host"},
    )

    assert built == {BOTH: "literal"}


@pytest.mark.unit
def test_a_pass_through_name_the_host_lacks_is_skipped_by_name(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``pass_env=("CI",)`` has to stay usable on a laptop (ADR-0013)."""
    with caplog.at_level(logging.DEBUG, logger="waystation"):
        built = allowlisted_env(
            literal={}, pass_env=(HOST, "WAYSTATION_ABSENT"), host_env={HOST: "here"}
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
        host_env={"PATH": "theirs", "HOME": "/home/x"},
    )

    assert built == {"PATH": "mine", "HOME": "/home/x"}


def _env_printing_agent(
    *, env: Mapping[str, str] | None = None, pass_env: Sequence[str] = ()
) -> ShellAgent:
    """An agent that prints its own environment, then reports."""
    return ShellAgent(
        f"env\necho '{OK_OUTCOME_LINE}'",
        env=dict(env or {}),
        pass_env=tuple(pass_env),
    )


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
        other.append((await ctx.sandbox.exec([*ctx.sandbox.shell, "env"])).stdout)

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
@pytest.mark.parametrize("when", ["workspace_ready", "sandbox_ready"])
async def test_the_sandbox_and_the_agent_see_one_host_environment(
    host_repo: Path, monkeypatch: pytest.MonkeyPatch, when: str
) -> None:
    """A run never straddles two readings of the host.

    Moved before the sandbox starts, both see the new value; moved after,
    both still see the old one. What must never happen is the two
    disagreeing, which is what reading ``os.environ`` per call would give.
    """
    monkeypatch.setenv(MOVES, "before")
    agent_lines: list[str] = []
    sandbox_env: list[str] = []

    async def keep(ctx: RunContext, line: AgentLine) -> None:
        agent_lines.append(line.raw)

    def move_it(ctx: RunContext) -> None:
        os.environ[MOVES] = "after"

    async def read_the_sandbox(ctx: RunContext) -> None:
        sandbox_env.append((await ctx.sandbox.exec([*ctx.sandbox.shell, "env"])).stdout)

    spec = (
        Flow(
            host_repo,
            agent=_env_printing_agent(pass_env=(MOVES,)),
            sandbox=NoSandbox(pass_env=(MOVES,)),
        )
        .run("print it", outcome=Summary)
        .on_agent_output(keep)
        .on_sandbox_ready(read_the_sandbox)
    )
    spec = (
        spec.on_workspace_ready(move_it)
        if when == "workspace_ready"
        else spec.on_sandbox_ready(move_it)
    )

    result = await spec

    assert isinstance(result, RunSucceeded), result
    agent = _as_env("\n".join(agent_lines))[MOVES]
    sandbox = _as_env(sandbox_env[0])[MOVES]
    assert agent == sandbox, "the sandbox and its agent read the host separately"
    assert agent == ("after" if when == "workspace_ready" else "before")


RUN = "WAYSTATION_RUN"
RUN_HOST = "WAYSTATION_RUN_HOST"
TIERED = "WAYSTATION_TIERED"
UNNAMED = "WAYSTATION_UNNAMED"


def _spec(
    host_repo: Path, *, sandbox: NoSandbox, agent: ShellAgent
) -> RunSpec[Summary]:
    return Flow(host_repo, agent=agent, sandbox=sandbox).run(
        "print the environment", outcome=Summary
    )


async def _environments(
    spec: RunSpec[Summary],
) -> tuple[dict[str, str], dict[str, str]]:
    """What ``spec``'s agent exec saw, and what an exec outside it saw."""
    agent_lines: list[str] = []
    other: list[str] = []

    async def keep(ctx: RunContext, line: AgentLine) -> None:
        agent_lines.append(line.raw)

    async def env_outside(ctx: RunContext) -> None:
        other.append((await ctx.sandbox.exec([*ctx.sandbox.shell, "env"])).stdout)

    result = await spec.on_agent_output(keep).on_sandbox_ready(env_outside)

    assert isinstance(result, RunSucceeded), result
    return _as_env("\n".join(agent_lines)), _as_env(other[0])


@pytest.mark.git
async def test_a_per_run_environment_reaches_every_exec(
    host_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unlike a provider's, it goes in at the start, so collect sees it too."""
    monkeypatch.setenv(RUN_HOST, "from-host")

    inside, outside = await _environments(
        _spec(host_repo, sandbox=NoSandbox(), agent=_env_printing_agent())
        .env({RUN: "run"})
        .pass_env(RUN_HOST)
    )

    for seen in (inside, outside):
        assert seen[RUN] == "run"
        assert seen[RUN_HOST] == "from-host"


@pytest.mark.git
@pytest.mark.parametrize(
    ("spec_tier", "provider_tier", "run_tier", "wins"),
    [
        ("spec", None, None, "spec"),
        ("spec", "provider", None, "provider"),
        ("spec", "provider", "run", "run"),
        ("spec", None, "run", "run"),
        (None, "provider", "run", "run"),
    ],
)
async def test_the_most_specific_tier_wins_at_the_agent_exec(
    host_repo: Path,
    spec_tier: str | None,
    provider_tier: str | None,
    run_tier: str | None,
    wins: str,
) -> None:
    """Spec, then provider, then per run (ADR-0013), for one variable."""

    def at(value: str | None) -> dict[str, str]:
        return {} if value is None else {TIERED: value}

    inside, outside = await _environments(
        _spec(
            host_repo,
            sandbox=NoSandbox(env=at(spec_tier)),
            agent=_env_printing_agent(env=at(provider_tier)),
        ).env(at(run_tier))
    )

    assert inside[TIERED] == wins
    # Outside the agent exec the provider's tier is absent, so the ranking
    # there is the same one with the provider taken out.
    outside_wins = run_tier or spec_tier
    assert outside.get(TIERED) == outside_wins


@pytest.mark.git
async def test_a_per_run_pass_through_name_beats_a_providers_literal(
    host_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tiers rank before kinds: a literal beats a name only within one tier."""
    monkeypatch.setenv(TIERED, "from-host")

    inside, _ = await _environments(
        _spec(
            host_repo,
            sandbox=NoSandbox(),
            agent=_env_printing_agent(env={TIERED: "provider"}),
        ).pass_env(TIERED)
    )

    assert inside[TIERED] == "from-host"


@pytest.mark.git
async def test_nothing_the_host_holds_reaches_an_exec_unless_named(
    host_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(UNNAMED, "secret")
    monkeypatch.setenv(RUN_HOST, "named")

    inside, outside = await _environments(
        _spec(
            host_repo,
            sandbox=NoSandbox(env={SPEC: "spec"}),
            agent=_env_printing_agent(env={AGENT: "agent"}),
        )
        .env({RUN: "run"})
        .pass_env(RUN_HOST)
    )

    for seen in (inside, outside):
        assert UNNAMED not in seen
        assert "secret" not in seen.values()


@pytest.mark.git
async def test_a_per_run_pass_through_name_is_read_when_the_run_starts(
    host_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resolved from the host at launch, not when the spec was built."""
    monkeypatch.setenv(RUN_HOST, "at-build")
    spec = _spec(host_repo, sandbox=NoSandbox(), agent=_env_printing_agent()).pass_env(
        RUN_HOST
    )
    monkeypatch.setenv(RUN_HOST, "at-launch")

    inside, outside = await _environments(spec)

    assert inside[RUN_HOST] == "at-launch"
    assert outside[RUN_HOST] == "at-launch"


@pytest.mark.git
async def test_a_per_run_author_is_the_author_of_the_agents_commits(
    host_repo: Path,
) -> None:
    """The workspace copies the host identity; a per-run author overrides it."""
    agent = ShellAgent(
        "echo x > authored.txt && git add authored.txt"
        " && git commit -q -m authored"
        f"\necho '{OK_OUTCOME_LINE}'"
    )

    result = await (
        Flow(host_repo, agent=agent, sandbox=NoSandbox())
        .run("commit something", outcome=Summary)
        .env({"GIT_AUTHOR_NAME": "Per Run", "GIT_AUTHOR_EMAIL": "run@example.com"})
        .integrate("agents/authored")
    )

    assert isinstance(result, RunSucceeded), result
    author = git(host_repo, "log", "-1", "--format=%an <%ae>", "agents/authored")
    assert author == "Per Run <run@example.com>"
