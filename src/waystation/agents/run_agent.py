"""Run an agent command inside a sandbox and validate its Outcome."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import os
import time
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from typing import Any, Literal

from pydantic import TypeAdapter, ValidationError

from waystation._outcome import outcome_schema
from waystation.agents.protocol import (
    AgentCommand,
    AgentLine,
    AgentProvider,
    OutcomeReported,
)
from waystation.clock import get_clock
from waystation.errors import StageError
from waystation.observability import AGENT
from waystation.results import (
    AgentExit,
    AgentExited,
    AgentUsage,
    OutcomeInvalid,
    OutcomeMissing,
    TimedOut,
    Timeouts,
)
from waystation.sandbox import allowlisted_env
from waystation.sandbox.protocol import ExecResult, Sandbox

__all__ = ["run_agent"]

_BoundName = Literal["agent_silence", "agent_wall", "completion_grace"]


class _AgentBound(Exception):
    """Internal: an agent-stage bound fired; may be failure or hanging success."""

    def __init__(
        self,
        bound: _BoundName,
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


# What a POSIX shell exits when it cannot find the command it was asked to
# run. An agent is free to exit 127 meaning something else, so this reads as
# a likely cause rather than a verdict.
_NOT_FOUND = 127


def run_agent[OutcomeT](
    sandbox: Sandbox,
    provider: AgentProvider,
    prompt: str,
    outcome_type: type[OutcomeT],
    *,
    timeouts: Timeouts | None = None,
    on_output: Callable[[AgentLine], Awaitable[None] | None] | None = None,
    host_env: Mapping[str, str] | None = None,
    env: Mapping[str, str] | None = None,
) -> Coroutine[Any, Any, tuple[AgentExit, OutcomeT]]:
    """Run ``prompt`` through ``provider`` and validate the Outcome it reports.

    ``outcome_type`` is the whole Outcome contract: the schema the provider
    is asked to deliver is derived from it, and every report is validated
    against it, so the two cannot disagree (ADR-0019). The provider
    builds its command here, when ``run_agent`` is called; awaiting the
    result execs it, parses stdout lines and returns the last valid report
    (ADR-0038).

    ``on_output`` receives every stdout and stderr line as an ``AgentLine``,
    awaited before the next line is read.

    ``host_env`` is the host environment the provider's ``pass_env`` names are
    looked up in; a run passes the one it read at its start, so one run sees
    one environment however long it takes (ADR-0034). On its own it reads
    ``os.environ``, as running a command by hand would.

    ``env`` is resolved literals laid over the provider's tier at the agent
    exec. A run passes its per-run tier here, since the sandbox already
    holds that tier but the provider's would otherwise win over it at this
    one exec — and the most specific tier wins everywhere (ADR-0013).

    Raises ``TypeError`` at the call for an ``outcome_type`` that is not
    object-shaped, before the provider is asked for anything, and an
    ``Exception`` from ``provider.command`` propagates from the call
    unchanged: nothing has started, so there is nothing to await.

    Awaiting raises ``StageError`` for agent-stage failures (non-zero exit,
    missing/invalid Outcome, silence/wall timeout). An ``Exception`` from
    ``parse`` or ``on_output`` cancels the exec, then propagates unchanged.
    """
    command = provider.command(prompt, outcome_schema(outcome_type))
    return _run(
        sandbox,
        provider,
        command,
        outcome_type,
        timeouts=timeouts,
        on_output=on_output,
        host_env=host_env,
        env=env,
    )


async def _run[OutcomeT](
    sandbox: Sandbox,
    provider: AgentProvider,
    command: AgentCommand,
    outcome_type: type[OutcomeT],
    *,
    timeouts: Timeouts | None,
    on_output: Callable[[AgentLine], Awaitable[None] | None] | None,
    host_env: Mapping[str, str] | None,
    env: Mapping[str, str] | None,
) -> tuple[AgentExit, OutcomeT]:
    """Exec ``command`` and validate what it reports; see ``run_agent``."""
    bounds = timeouts if timeouts is not None else Timeouts()
    adapter = TypeAdapter(outcome_type)
    last_valid: OutcomeT | None = None
    last_invalid: tuple[Any, ValidationError] | None = None
    # Usage is cumulative per report, so the last one is the run's total.
    last_usage: AgentUsage | None = None
    started = time.perf_counter()
    clock = get_clock()
    silence_task: asyncio.Task[None] | None = None
    grace_task: asyncio.Task[None] | None = None
    # Grace still owed once an Outcome is reported (None until then). Timers
    # pause while a line is delivered, so hook time is never agent silence.
    grace_left: float | None = None
    grace_since = 0.0
    exec_task: asyncio.Task[ExecResult] | None = None
    bound_event = asyncio.Event()
    fired: _AgentBound | None = None
    line_error: Exception | None = None
    # stdout and stderr are read concurrently; lines are delivered one at a time.
    delivering = asyncio.Lock()
    t_origin = clock.monotonic()

    def _agent_exit(exit_code: int, *, hanging: bool = False) -> AgentExit:
        return AgentExit(
            exit_code=exit_code,
            elapsed=time.perf_counter() - started,
            hanging=hanging,
            usage=last_usage,
        )

    def _cancel(task: asyncio.Task[Any] | None) -> None:
        if task is not None and not task.done():
            task.cancel()

    def _fire(bound: _BoundName, limit: float, *, is_hanging: bool = False) -> None:
        nonlocal fired
        if fired is not None or line_error is not None:
            return
        elapsed = clock.monotonic() - t_origin
        fired = _AgentBound(bound, limit=limit, elapsed=elapsed, hanging=is_hanging)
        bound_event.set()
        _cancel(exec_task)

    def _arm_silence() -> None:
        nonlocal silence_task
        _cancel(silence_task)
        if bounds.agent_silence is None:
            return
        limit = bounds.agent_silence

        async def _silence() -> None:
            await clock.sleep(limit)
            _fire("agent_silence", limit)

        silence_task = asyncio.create_task(_silence(), name="agent-silence")

    def _arm_grace(seconds: float) -> None:
        nonlocal grace_task, grace_since
        grace_since = clock.monotonic()
        limit = bounds.completion_grace

        async def _grace() -> None:
            await clock.sleep(seconds)
            _fire("completion_grace", limit, is_hanging=True)

        grace_task = asyncio.create_task(_grace(), name="completion-grace")

    def _pause_timers() -> None:
        nonlocal grace_task, grace_left
        _cancel(silence_task)
        if grace_task is not None and grace_left is not None:
            grace_task.cancel()
            grace_task = None
            grace_left -= clock.monotonic() - grace_since

    def _resume_timers() -> None:
        # After an Outcome, only completion_grace runs — silence stays off.
        if grace_left is None:
            _arm_silence()
        else:
            _arm_grace(max(grace_left, 0.0))

    async def _deliver(handler: Callable[[], Awaitable[None]]) -> None:
        # A raising parser or on_output stops the agent: cancel the exec
        # instead of letting the error strand the pipe readers.
        nonlocal line_error
        async with delivering:
            if fired is not None or line_error is not None:
                return
            _pause_timers()
            try:
                await handler()
            except Exception as exc:
                line_error = exc
                bound_event.set()
                _cancel(exec_task)
                return
            _resume_timers()

    async def _emit(line: AgentLine) -> None:
        if on_output is None:
            return
        returned = on_output(line)
        if inspect.isawaitable(returned):
            await returned

    async def _stdout_line(line: str) -> None:
        nonlocal last_valid, last_invalid, grace_left, last_usage
        events = tuple(provider.parse(line))
        for event in events:
            if isinstance(event, AgentUsage):
                last_usage = event
            elif isinstance(event, OutcomeReported):
                try:
                    last_valid = adapter.validate_python(event.raw)
                    last_invalid = None
                except ValidationError as exc:
                    last_invalid = (event.raw, exc)
                else:
                    if grace_left is None:
                        grace_left = bounds.completion_grace
        await _emit(AgentLine("stdout", line, events))

    async def on_stdout(line: str) -> None:
        await _deliver(lambda: _stdout_line(line))

    async def on_stderr(line: str) -> None:
        await _deliver(lambda: _emit(AgentLine("stderr", line)))

    # The provider's tier, resolved where every tier is (ADR-0034). A run
    # passes the host it read at its start, so one run sees one environment.
    exec_env = {
        **allowlisted_env(
            literal=command.env,
            pass_env=command.pass_env,
            host_env=os.environ if host_env is None else host_env,
        ),
        **(env or {}),
    }

    _arm_silence()

    async def _exec() -> ExecResult:
        # A script needs the sandbox's shell; an argv does not, so it is not
        # asked for one. A sandbox may resolve its shell only when read, and
        # a host that has none still runs a provider that binds a CLI (#101).
        argv = (
            command.argv if command.script is None else command.argv_in(sandbox.shell)
        )
        return await sandbox.exec(
            list(argv),
            stdin=command.stdin,
            env=exec_env,
            capture=False,
            on_stdout=on_stdout,
            on_stderr=on_stderr,
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
        if line_error is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await exec_task
            raise line_error
        if fired is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await exec_task
            if fired.hanging and last_valid is not None:
                return _agent_exit(-1, hanging=True), last_valid
            raise StageError(
                "agent",
                TimedOut(
                    bound=fired.bound,
                    limit=fired.limit,
                    elapsed=fired.elapsed,
                ),
                # No exit code, but what the agent spent before it was
                # stopped is still worth reporting.
                agent=_agent_exit(-1),
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
    agent_exit = _agent_exit(result.exit_code)
    if result.exit_code == _NOT_FOUND:
        # Preflight cannot rule this out: what a sandbox holds is only
        # knowable inside it, and looking would cost a sandbox start per
        # (agent, sandbox) pair, which ADR-0018 keeps preflight out of. So
        # the run says it here instead (ADR-0036).
        AGENT.error(
            "the agent exited 127, which is what a shell exits for a command "
            "it could not find. If that is what happened, the sandbox has to "
            "bring it: waystation never installs, builds or pulls anything "
            "(ADR-0011). It said: %s",
            result.stderr.strip() or "nothing",
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
