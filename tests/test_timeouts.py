"""Timeouts, silence, completion grace, and exec cancellation (#26)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from helpers import USAGE, ShellAgent, awaited, until
from waystation import (
    Flow,
    NoSandbox,
    RunFailed,
    RunSucceeded,
    ScriptedAgent,
    ScriptedCommit,
    TimedOut,
    Timeouts,
)
from waystation.agents import AgentUsage
from waystation.agents.outcome import OUTCOME_MARKER
from waystation.agents.protocol import AgentCommand, AgentEvent
from waystation.agents.scripted import ScriptedAgent as ScriptedParser
from waystation.clock import ManualClock, get_clock, use_clock
from waystation.sandbox.protocol import ExecResult, LineCallback, Sandbox
from waystation.workspace import Workspace


class Answer(BaseModel):
    summary: str


async def _drive(clock: ManualClock, coro: Any, steps: Sequence[float]) -> Any:
    """Run ``coro`` under ``clock``, advancing through ``steps`` while it runs."""
    with use_clock(clock):
        task = asyncio.create_task(coro)

        async def _wait_for_park(*, label: str) -> None:
            deadline = asyncio.get_running_loop().time() + 5.0
            while True:
                if task.done():
                    return
                if clock._waiters:
                    return
                if asyncio.get_running_loop().time() > deadline:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
                    raise AssertionError(f"run never parked on ManualClock ({label})")
                await asyncio.sleep(0.01)

        for i, delta in enumerate(steps):
            await _wait_for_park(label=f"step {i} before +{delta}")
            if task.done():
                break
            clock.advance(delta)
            await asyncio.sleep(0)
        return await task


@pytest.mark.unit
def test_timeouts_defaults_are_unbounded_except_completion_grace() -> None:
    t = Timeouts()
    assert t.workspace is None
    assert t.sandbox is None
    assert t.agent_silence is None
    assert t.agent_wall is None
    assert t.collect is None
    assert t.integrate is None
    assert t.teardown is None
    assert t.completion_grace == 30.0


@pytest.mark.unit
def test_flow_timeouts_replaced_per_run(host_repo: Path) -> None:
    flow = Flow(
        host_repo,
        agent=ScriptedAgent(outcome=Answer(summary="x")),
        sandbox=NoSandbox(),
        timeouts=Timeouts(agent_wall=60.0),
    )
    spec = flow.run("p", outcome=Answer).with_timeouts(Timeouts(agent_silence=5.0))
    assert spec.timeouts.agent_silence == 5.0
    assert spec.timeouts.agent_wall is None
    assert flow.timeouts.agent_wall == 60.0


@dataclass(slots=True)
class _ClockExecSandbox:
    workspace: str
    lines: Sequence[tuple[float, str]]
    hang_after: float | None = None
    hang_git: float | None = None
    exit_code: int = 0

    async def exec(
        self,
        argv: Sequence[str],
        *,
        stdin: str | None = None,
        env: Mapping[str, str] | None = None,
        capture: bool = True,
        on_stdout: LineCallback | None = None,
        on_stderr: LineCallback | None = None,
    ) -> ExecResult:
        del stdin, env, on_stderr
        clock = get_clock()
        # Only the agent command is scripted; collect/git execs succeed instantly
        # unless hang_git is set (stage-bound tests).
        if tuple(argv) != ("true",):
            if self.hang_git is not None:
                await clock.sleep(self.hang_git)
            return ExecResult(exit_code=0, stdout="", stderr="")
        for delay, line in self.lines:
            await clock.sleep(delay)
            if on_stdout is not None:
                maybe = on_stdout(line)
                if asyncio.iscoroutine(maybe):
                    await maybe
        if self.hang_after is not None:
            await clock.sleep(self.hang_after)
        return ExecResult(exit_code=self.exit_code, stdout="", stderr="")


@dataclass(frozen=True, slots=True)
class ClockSandbox:
    """Sandbox whose stdout schedule is driven by the active ManualClock."""

    lines: tuple[tuple[float, str], ...] = ()
    hang_after: float | None = None
    hang_git: float | None = None
    exit_code: int = 0

    async def preflight(self) -> None:
        return None

    @asynccontextmanager
    async def start(
        self,
        ws: Workspace,
        *,
        env: Mapping[str, str],
    ) -> AsyncIterator[Sandbox]:
        del env
        box: Sandbox = _ClockExecSandbox(
            workspace=str(ws.path),
            lines=self.lines,
            hang_after=self.hang_after,
            hang_git=self.hang_git,
            exit_code=self.exit_code,
        )
        yield box


@dataclass(frozen=True, slots=True)
class ParseOnlyAgent:
    """Command is ignored; ClockSandbox supplies stdout lines."""

    def preflight(self) -> None:
        return None

    def command(self, prompt: str, outcome_schema: dict[str, Any]) -> AgentCommand:
        del prompt, outcome_schema
        return AgentCommand(argv=("true",), stdin=None, env={}, pass_env=())

    def parse(self, line: str) -> Sequence[AgentEvent]:
        if line.startswith(USAGE):
            return ShellAgent("").parse(line)
        return ScriptedParser().parse(line)


@pytest.mark.git
@pytest.mark.asyncio
async def test_agent_silence_fails_when_silent(host_repo: Path) -> None:
    clock = ManualClock()
    flow = Flow(
        host_repo,
        agent=ParseOnlyAgent(),
        sandbox=ClockSandbox(
            lines=((5.0, f'{OUTCOME_MARKER} {{"summary": "late"}}'),),
        ),
        timeouts=Timeouts(agent_silence=1.0),
    )
    result = await _drive(clock, awaited(flow.run("p", outcome=Answer)), steps=(1.0,))
    assert isinstance(result, RunFailed)
    assert result.stage == "agent"
    assert isinstance(result.failure, TimedOut)
    assert result.failure.bound == "agent_silence"
    assert result.failure.limit == 1.0


@pytest.mark.git
@pytest.mark.asyncio
async def test_agent_silence_resets_on_output_lines(host_repo: Path) -> None:
    clock = ManualClock()
    outcome = f'{OUTCOME_MARKER} {{"summary": "ok"}}'
    flow = Flow(
        host_repo,
        agent=ParseOnlyAgent(),
        sandbox=ClockSandbox(
            lines=((0.8, "tick"), (0.8, "tick"), (0.8, outcome)),
        ),
        timeouts=Timeouts(agent_silence=1.0, completion_grace=30.0),
    )
    result = await _drive(
        clock,
        awaited(flow.run("p", outcome=Answer)),
        steps=(0.8, 0.8, 0.8),
    )
    assert isinstance(result, RunSucceeded)
    assert result.outcome.summary == "ok"
    assert result.agent is not None
    assert result.agent.hanging is False


@pytest.mark.git
@pytest.mark.asyncio
async def test_agent_wall_bound_fails(host_repo: Path) -> None:
    clock = ManualClock()
    flow = Flow(
        host_repo,
        agent=ParseOnlyAgent(),
        sandbox=ClockSandbox(
            lines=((10.0, f'{OUTCOME_MARKER} {{"summary": "late"}}'),),
        ),
        timeouts=Timeouts(agent_wall=2.0),
    )
    result = await _drive(clock, awaited(flow.run("p", outcome=Answer)), steps=(2.0,))
    assert isinstance(result, RunFailed)
    assert result.stage == "agent"
    assert isinstance(result.failure, TimedOut)
    assert result.failure.bound == "agent_wall"


@pytest.mark.git
@pytest.mark.asyncio
async def test_completion_grace_succeeds_hanging(host_repo: Path) -> None:
    clock = ManualClock()
    outcome = f'{OUTCOME_MARKER} {{"summary": "done"}}'
    flow = Flow(
        host_repo,
        agent=ParseOnlyAgent(),
        sandbox=ClockSandbox(lines=((0.0, outcome),), hang_after=100.0),
        timeouts=Timeouts(completion_grace=1.0),
    )
    result = await _drive(
        clock, awaited(flow.run("p", outcome=Answer)), steps=(0.0, 1.0)
    )
    assert isinstance(result, RunSucceeded)
    assert result.outcome.summary == "done"
    assert result.agent is not None
    assert result.agent.hanging is True


USED = AgentUsage(input_tokens=40, output_tokens=9, cost_usd=0.02, turns=2)
USED_LINE = USAGE + json.dumps(asdict(USED))


@pytest.mark.git
async def test_a_timed_out_agent_keeps_the_usage_it_reported(host_repo: Path) -> None:
    clock = ManualClock()
    flow = Flow(
        host_repo,
        agent=ParseOnlyAgent(),
        sandbox=ClockSandbox(lines=((0.0, USED_LINE), (10.0, "late"))),
        # The wall, not silence: silence re-arms on the usage line, and a
        # re-armed timer can park after the clock has already moved.
        timeouts=Timeouts(agent_wall=2.0),
    )
    result = await _drive(clock, awaited(flow.run("p", outcome=Answer)), steps=(2.0,))
    assert isinstance(result, RunFailed)
    assert isinstance(result.failure, TimedOut)
    assert result.agent is not None
    assert result.agent.usage == USED


@pytest.mark.git
async def test_a_hanging_agent_keeps_the_usage_it_reported(host_repo: Path) -> None:
    clock = ManualClock()
    outcome = f'{OUTCOME_MARKER} {{"summary": "done"}}'
    flow = Flow(
        host_repo,
        agent=ParseOnlyAgent(),
        sandbox=ClockSandbox(
            lines=((0.0, USED_LINE), (0.0, outcome)), hang_after=100.0
        ),
        timeouts=Timeouts(completion_grace=1.0),
    )
    result = await _drive(
        clock, awaited(flow.run("p", outcome=Answer)), steps=(0.0, 0.0, 1.0)
    )
    assert isinstance(result, RunSucceeded)
    assert result.agent is not None
    assert result.agent.hanging is True
    assert result.agent.usage == USED


@pytest.mark.unit
def test_scripted_delay_and_linger_in_command() -> None:
    agent = ScriptedAgent(
        outcome=Answer(summary="x"),
        delay=1.5,
        linger=True,
    )
    assert agent.delay == 1.5
    assert agent.linger is True
    script = agent.command("p", {}).argv[-1]
    assert "sleep 1.5" in script
    assert "&" in script


@pytest.mark.git
@pytest.mark.asyncio
async def test_cancel_kills_grandchild_writer(host_repo: Path, tmp_path: Path) -> None:
    """Cancelling exec kills the process tree — grandchild cannot keep writing."""
    pulse = tmp_path / "pulse"
    agent = ScriptedAgent(
        outcome=Answer(summary="ok"),
        uncommitted={"note.txt": "work\n"},
        linger=True,
        linger_touch=str(pulse),
    )
    flow = Flow(
        host_repo,
        agent=agent,
        sandbox=NoSandbox(),
        timeouts=Timeouts(completion_grace=0.05),
        salvage=True,
    )
    t0 = time.perf_counter()
    result = await flow.run("p", outcome=Answer)
    # Under xdist, kill+collect can exceed 2s; hang/pulse are the real signal.
    assert time.perf_counter() - t0 < 15.0
    assert isinstance(result, RunSucceeded)
    assert result.agent is not None
    assert result.agent.hanging is True
    assert result.series is not None
    assert result.series.commits >= 1
    assert result.preserved is not None
    # Grandchild must be dead: pulse file stops growing.
    await asyncio.sleep(0.15)
    size_a = pulse.stat().st_size if pulse.exists() else 0
    await asyncio.sleep(0.15)
    size_b = pulse.stat().st_size if pulse.exists() else 0
    assert size_b == size_a


@pytest.mark.git
@pytest.mark.asyncio
async def test_collect_bound_times_out(host_repo: Path) -> None:
    clock = ManualClock()
    outcome = f'{OUTCOME_MARKER} {{"summary": "ok"}}'
    flow = Flow(
        host_repo,
        agent=ParseOnlyAgent(),
        sandbox=ClockSandbox(lines=((0.0, outcome),), hang_git=100.0),
        timeouts=Timeouts(collect=1.0, completion_grace=30.0),
    )
    with use_clock(clock):
        task = asyncio.create_task(awaited(flow.run("p", outcome=Answer)))
        # The agent's completion grace parks and goes again, and collect's
        # git parks for 100 s: advance only once collect's own bound has
        # parked, whatever order the others came and went in.
        await until(
            lambda: any(due <= clock.monotonic() + 1.0 for due, _ in clock._waiters),
            task,
        )
        clock.advance(1.0)
        result = await task
    assert isinstance(result, RunFailed)
    assert result.stage == "collect"
    assert isinstance(result.failure, TimedOut)
    assert result.failure.bound == "collect"


@pytest.mark.git
@pytest.mark.asyncio
async def test_wall_timeout_preserves_series(host_repo: Path, tmp_path: Path) -> None:
    """After a timeout, collect runs and a non-empty series is preserved."""
    # A scripted agent commits before it lingers, so the first byte of its
    # linger file says the commit landed. That is the signal the clock waits
    # for, and asking the filesystem is a stat: a poll that shelled out to git
    # every tick would starve the very run it is waiting on.
    committed = tmp_path / "committed"
    agent = ScriptedAgent(
        outcome=None,
        commits=(ScriptedCommit(message="wip", files={"a.txt": "a\n"}),),
        linger=True,
        linger_touch=str(committed),
    )
    clock = ManualClock()
    flow = Flow(
        host_repo,
        agent=agent,
        sandbox=NoSandbox(),
        timeouts=Timeouts(agent_wall=1.0),
        salvage=True,
    )
    with use_clock(clock):
        task = asyncio.create_task(awaited(flow.run("p", outcome=Answer)))
        # Wait until the wall sleeper is parked, then until the commit has
        # really landed. Advancing after an elapsed-time guess instead races
        # git: the bound fires first, the series comes back empty, and the
        # test fails on a loaded box for a reason it does not test.
        await until(lambda: bool(clock._waiters), task)
        await until(committed.exists, task)
        clock.advance(1.0)
        result = await task

    assert isinstance(result, RunFailed)
    assert result.stage == "agent"
    assert isinstance(result.failure, TimedOut)
    assert result.failure.bound == "agent_wall"
    assert result.series is not None
    assert result.series.commits >= 1
    assert result.preserved is not None
