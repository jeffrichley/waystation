"""Run an agent command inside a sandbox and validate its Outcome."""

from __future__ import annotations

import os
import time
from typing import Any

from pydantic import TypeAdapter, ValidationError

from waystation.agents.protocol import AgentCommand, AgentProvider, OutcomeReported
from waystation.errors import StageError
from waystation.results import AgentExit, AgentExited, OutcomeInvalid, OutcomeMissing
from waystation.sandbox.protocol import Sandbox


async def run_agent[OutcomeT](
    sandbox: Sandbox,
    provider: AgentProvider,
    command: AgentCommand,
    outcome_type: type[OutcomeT],
) -> tuple[AgentExit, OutcomeT]:
    """Exec ``command``, parse stdout lines, validate the last valid Outcome report.

    Raises ``StageError`` for agent-stage failures (non-zero exit, missing/invalid
    Outcome). Unexpected ``Exception`` from ``parse`` propagates for the orchestrator
    to wrap as ``Errored``.
    """
    adapter = TypeAdapter(outcome_type)
    last_valid: OutcomeT | None = None
    last_invalid: tuple[Any, ValidationError] | None = None
    started = time.perf_counter()

    async def on_stdout(line: str) -> None:
        nonlocal last_valid, last_invalid
        for event in provider.parse(line):
            if isinstance(event, OutcomeReported):
                try:
                    last_valid = adapter.validate_python(event.raw)
                    last_invalid = None
                except ValidationError as exc:
                    last_invalid = (event.raw, exc)

    exec_env = dict(command.env)
    for key in command.pass_env:
        value = os.environ.get(key)
        if value is not None:
            exec_env[key] = value
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
    if result.exit_code != 0:
        raise StageError(
            "agent",
            AgentExited(
                exit_code=result.exit_code,
                stdout_tail=result.stdout,
                stderr_tail=result.stderr,
                outcome=last_valid,
            ),
            agent=agent_exit,
        )
    if last_valid is not None:
        return agent_exit, last_valid
    if last_invalid is not None:
        raw, error = last_invalid
        raise StageError(
            "agent",
            OutcomeInvalid(raw=raw, error=error),
            agent=agent_exit,
        )
    raise StageError(
        "agent",
        OutcomeMissing(stdout_tail=result.stdout),
        agent=agent_exit,
    )
