"""Run an agent command inside a sandbox and validate its Outcome."""

from __future__ import annotations

import time
from typing import Any

from pydantic import TypeAdapter

from waystation.agents.protocol import AgentCommand, AgentProvider, OutcomeReported
from waystation.results import AgentExit
from waystation.sandbox.protocol import Sandbox


async def run_agent[OutcomeT](
    sandbox: Sandbox,
    provider: AgentProvider,
    command: AgentCommand,
    outcome_type: type[OutcomeT],
) -> tuple[AgentExit, OutcomeT]:
    """Exec ``command``, parse stdout lines, validate the last Outcome report."""
    adapter = TypeAdapter(outcome_type)
    last_raw: Any | None = None
    started = time.perf_counter()

    async def on_stdout(line: str) -> None:
        nonlocal last_raw
        for event in provider.parse(line):
            if isinstance(event, OutcomeReported):
                last_raw = event.raw

    exec_env = dict(command.env)
    result = await sandbox.exec(
        list(command.argv),
        stdin=command.stdin,
        env=exec_env,
        capture=False,
        on_stdout=on_stdout,
    )
    elapsed = time.perf_counter() - started
    agent_exit = AgentExit(
        exit_code=result.exit_code,
        elapsed=elapsed,
        hanging=False,
    )
    if last_raw is None:
        msg = "agent reported no Outcome"
        raise RuntimeError(msg)
    outcome = adapter.validate_python(last_raw)
    return agent_exit, outcome
