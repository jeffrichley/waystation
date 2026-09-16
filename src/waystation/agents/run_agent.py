"""Run an agent command inside a sandbox and validate its Outcome."""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from typing import Any, Literal

from pydantic import TypeAdapter, ValidationError

from waystation.agents.protocol import AgentCommand, AgentProvider, OutcomeReported
from waystation.clock import get_clock
from waystation.errors import StageError
from waystation.results import (
    AgentExit,
    AgentExited,
    OutcomeInvalid,
    OutcomeMissing,
    TimedOut,
    Timeouts,
)
from waystation.sandbox.protocol import ExecResult, Sandbox

BoundName = Literal["agent_silence", "agent_wall", "completion_grace"]


class _AgentBound(Exception):
    """Internal: an agent-stage bound fired; may be failure or hanging success."""

    def __init__(
        self,
        bound: BoundName,
        *,
        limit: float,
        elapsed: float,
        hanging: bool = False,
    ) -> None:
        self.bound = bound
        self.limit = limit
        self.elapsed = elapsed
        self.hanging = hanging
        super().__init__(bound)


async def run_agent[OutcomeT](
    sandbox: Sandbox,
    provider: AgentProvider,
    command: AgentCommand,
    outcome_type: type[OutcomeT],
    *,
    timeouts: Timeouts | None = None,
) -> tuple[AgentExit, OutcomeT]:
    """Exec ``command``, parse stdout lines, validate the last valid Outcome report.

    Raises ``StageError`` for agent-stage failures (non-zero exit, missing/invalid
    Outcome, silence/wall timeout). Unexpected ``Exception`` from ``parse``
    propagates for the orchestrator to wrap as ``Errored``.
    """
    bounds = timeouts if timeouts is not None else Timeouts()
    adapter = TypeAdapter(outcome_type)
    last_valid: OutcomeT | None = None
    last_invalid: tuple[Any, ValidationError] | None = None
    started = time.perf_counter()
    clock = get_clock()
    silence_task: asyncio.Task[None] | None = None
    grace_task: asyncio.Task[None] | None = None
    exec_task: asyncio.Task[ExecResult] | None = None
    bound_event = asyncio.Event()
    fired: _AgentBound | None = None
    t_origin = clock.monotonic()

    def _fire(bound: BoundName, limit: float, *, is_hanging: bool = False) -> None:
        nonlocal fired
        if fired is not None:
            return
        elapsed = clock.monotonic() - t_origin
        fired = _AgentBound(
            bound, limit=limit, elapsed=elapsed, hanging=is_hanging
        )
        bound_event.set()
        if exec_task is not None and not exec_task.done():
            exec_task.cancel()

    def _arm_silence() -> None:
        nonlocal silence_task
        if bounds.agent_silence is None:
            return
        if silence_task is not None and not silence_task.done():
            silence_task.cancel()

        async def _silence() -> None:
            assert bounds.agent_silence is not None
            await clock.sleep(bounds.agent_silence)
            _fire("agent_silence", bounds.agent_silence)

        silence_task = asyncio.create_task(_silence(), name="agent-silence")

    def _arm_grace() -> None:
        nonlocal grace_task
        if grace_task is not None:
            return
        limit = bounds.completion_grace

        async def _grace() -> None:
            await clock.sleep(limit)
            _fire("completion_grace", limit, is_hanging=True)

        grace_task = asyncio.create_task(_grace(), name="completion-grace")

    async def on_stdout(line: str) -> None:
        nonlocal last_valid, last_invalid
        # After Outcome, only completion_grace runs — do not re-arm silence.
        if grace_task is None:
            _arm_silence()
        for event in provider.parse(line):
            if isinstance(event, OutcomeReported):
                try:
                    last_valid = adapter.validate_python(event.raw)
                    last_invalid = None
                except ValidationError as exc:
                    last_invalid = (event.raw, exc)
                else:
                    if silence_task is not None and not silence_task.done():
                        silence_task.cancel()
                    _arm_grace()

    exec_env = dict(command.env)
    for key in command.pass_env:
        value = os.environ.get(key)
        if value is not None:
            exec_env[key] = value

    _arm_silence()

    async def _exec() -> ExecResult:
        return await sandbox.exec(
            list(command.argv),
            stdin=command.stdin,
            env=exec_env,
            capture=False,
            on_stdout=on_stdout,
        )

    exec_task = asyncio.create_task(_exec(), name="agent-exec")

    async def _watch_wall() -> None:
        if bounds.agent_wall is None:
            await asyncio.Event().wait()
            return
        await clock.sleep(bounds.agent_wall)
        _fire("agent_wall", bounds.agent_wall)

    wall_task = asyncio.create_task(_watch_wall(), name="agent-wall")
    bound_waiter = asyncio.create_task(bound_event.wait(), name="bound-wait")

    result: ExecResult | None = None
    try:
        done, _pending = await asyncio.wait(
            {exec_task, wall_task, bound_waiter},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if fired is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await exec_task
            if fired.hanging and last_valid is not None:
                elapsed = time.perf_counter() - started
                return (
                    AgentExit(
                        exit_code=-1,
                        elapsed=elapsed,
                        hanging=True,
                    ),
                    last_valid,
                )
            raise StageError(
                "agent",
                TimedOut(
                    bound=fired.bound,
                    limit=fired.limit,
                    elapsed=fired.elapsed,
                ),
            )
        if exec_task in done:
            result = exec_task.result()
        else:
            result = await exec_task
    except asyncio.CancelledError:
        if exec_task is not None and not exec_task.done():
            exec_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await exec_task
        raise
    finally:
        for task in (silence_task, grace_task, wall_task, bound_waiter):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    assert result is not None
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
