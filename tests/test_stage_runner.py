"""A loop composed by hand gets what an awaited run gets (#70, ADR-0032).

The five stages are public primitives so a flow script can drive its own
loop, but the guarantees that make a run safe — a stage's bound, a
cancellation held until that stage's own work has ended, the time each stage
took — used to live only inside the orchestrator. ``stages()`` is where they
live now, so these tests compose the primitives the way a user would and
hold that loop to the same promises.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from helpers import (
    MAKES_A_MERGE,
    WORKS_UNTIL_STOPPED,
    ShellAgent,
    branches,
    git,
    subjects,
    until,
    workspaces,
)
from waystation import (
    Integration,
    NoSandbox,
    PatchSeries,
    Refused,
    ScriptedAgent,
    ScriptedCommit,
    StageError,
    Summary,
    TimedOut,
    Timeouts,
    collect,
    integrate,
    prepare_workspace,
    preserve_series,
    run_agent,
    stages,
)
from waystation.agents import AgentLine
from waystation.clock import ManualClock, use_clock
from waystation.results import Stage


class Answer(BaseModel):
    summary: str


async def _never() -> str:
    """Work that never finishes on its own; only a bound ends it."""
    await asyncio.Event().wait()
    return "unreachable"


async def _nothing() -> str:
    return "nothing"


async def _fire(clock: ManualClock, task: asyncio.Task[Any], seconds: float) -> None:
    """Let the bound register its sleeper, then move the clock past it."""
    await until(lambda: bool(clock._waiters), task)
    clock.advance(seconds)


@pytest.mark.git
async def test_a_loop_composed_by_hand_lands_the_series(host_repo: Path) -> None:
    agent = ScriptedAgent(
        commits=(ScriptedCommit(message="by hand", files={"hand.txt": "made\n"}),),
        outcome=Answer(summary="done"),
    )

    async with stages() as run:
        ws = await run.stage("workspace", prepare_workspace(host_repo))
        async with run.entering("sandbox", NoSandbox().start(ws, env={})) as box:
            exit, outcome = await run.stage(
                "agent",
                run_agent(box, agent, agent.command("by hand", {}), Answer),
                bound=None,
            )
            series = await run.stage("collect", collect(box, ws))
        report = await run.stage(
            "integrate", integrate(host_repo, series, Integration("agents/by-hand"))
        )

    assert exit.exit_code == 0
    assert outcome == Answer(summary="done")
    assert series.commits == 1
    assert not series.salvaged
    assert report.landed
    assert git(host_repo, "log", "-1", "--format=%s", "agents/by-hand") == "by hand"


@pytest.mark.git
async def test_the_sandbox_is_gone_before_integrate_begins(host_repo: Path) -> None:
    """``entering`` nests, so what it entered is left before the stages after it."""
    agent = ScriptedAgent(
        commits=(ScriptedCommit(message="by hand", files={"hand.txt": "x\n"}),),
        outcome=Answer(summary="done"),
    )
    order: list[str] = []

    async def landing() -> str:
        order.append("integrate")
        return "landed"

    async with stages() as run:
        ws = await run.stage("workspace", prepare_workspace(host_repo))
        async with run.entering("sandbox", NoSandbox().start(ws, env={})) as box:
            await run.stage(
                "agent",
                run_agent(box, agent, agent.command("x", {}), Answer),
                bound=None,
            )
            await run.stage("collect", collect(box, ws))
        order.append("left the sandbox")
        await run.stage("integrate", landing())

    assert order == ["left the sandbox", "integrate"]
    assert not ws.path.exists(), "leaving the sandbox discarded the workspace"


@pytest.mark.git
async def test_collect_refuses_a_nonlinear_series_rather_than_squashing_it_quietly(
    host_repo: Path,
) -> None:
    """A hand-composed loop hears the refusal, so it cannot land the squash."""
    agent = ShellAgent(MAKES_A_MERGE)

    async with stages() as run:
        ws = await run.stage("workspace", prepare_workspace(host_repo))
        async with run.entering("sandbox", NoSandbox().start(ws, env={})) as box:
            await run.stage(
                "agent",
                run_agent(box, agent, agent.command("merge", {}), Summary),
                bound=None,
            )
            with pytest.raises(StageError) as raised:
                await run.stage("collect", collect(box, ws))

    err = raised.value
    assert err.stage == "collect"
    assert isinstance(err.failure, Refused)
    assert err.failure.reason == "nonlinear_series"
    # The squash rides along, so a caller can still keep the work.
    assert err.series is not None
    assert err.series.commits == 1


@pytest.mark.unit
async def test_a_bound_that_fires_names_the_timeouts_field_it_came_from() -> None:
    clock = ManualClock()

    async def compose() -> None:
        async with stages(Timeouts(collect=90.0)) as run:
            await run.stage("collect", _never())

    with use_clock(clock):
        task = asyncio.create_task(compose())
        await _fire(clock, task, 90.0)
        with pytest.raises(StageError) as raised:
            await task

    err = raised.value
    assert err.stage == "collect"
    assert isinstance(err.failure, TimedOut)
    assert err.failure.bound == "collect"
    assert err.failure.limit == 90.0
    assert err.failure.elapsed == 90.0


@pytest.mark.unit
async def test_leaving_a_stage_is_bounded_by_teardown_and_stays_that_stage() -> None:
    """The mismatch is now at a call site: ``teardown`` bounds a sandbox stage."""
    clock = ManualClock()

    async def compose() -> None:
        async with stages(Timeouts(teardown=5.0)) as run:
            await run.anyway("sandbox", _never(), bound="teardown")

    with use_clock(clock):
        task = asyncio.create_task(compose())
        await _fire(clock, task, 5.0)
        with pytest.raises(StageError) as raised:
            await task

    assert raised.value.stage == "sandbox"
    assert isinstance(raised.value.failure, TimedOut)
    assert raised.value.failure.bound == "teardown"


@pytest.mark.unit
async def test_a_bound_naming_no_timeouts_field_says_so_and_names_them() -> None:
    async with stages() as run:
        with pytest.raises(ValueError, match="no bound named 'agent' on Timeouts"):
            await run.stage("agent", _nothing())


@pytest.mark.unit
async def test_elapsed_is_measured_on_the_clock_seam() -> None:
    """``ManualClock`` drives it: the elapsed is the time the test advanced."""
    clock = ManualClock()
    started, go = asyncio.Event(), asyncio.Event()
    seen: dict[Stage, float] = {}

    async def waiting() -> str:
        started.set()
        await go.wait()
        return "done"

    async def compose() -> None:
        async with stages() as run:
            await run.stage("collect", waiting())
            seen.update(run.elapsed)

    with use_clock(clock):
        task = asyncio.create_task(compose())
        await until(started.is_set, task)
        clock.advance(12.5)
        go.set()
        await task

    assert seen == {"collect": 12.5}


@pytest.mark.unit
async def test_a_cancellation_held_during_a_stage_stops_the_next_one_beginning() -> (
    None
):
    ran: list[str] = []
    runner: list[Any] = []
    started, go = asyncio.Event(), asyncio.Event()

    async def first() -> str:
        started.set()
        await go.wait()
        ran.append("first")
        return "first"

    async def second() -> str:
        ran.append("second")
        return "second"

    async def compose() -> None:
        async with stages() as run:
            runner.append(run)
            await run.stage("workspace", first())
            await run.stage("collect", second())

    task = asyncio.create_task(compose())
    await until(started.is_set, task)
    task.cancel()
    await until(lambda: runner[0].cancelled_during is not None, task)
    go.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The stage under way ran to its end; the one after it never began.
    assert ran == ["first"]
    assert runner[0].cancelled_during == "workspace"


@pytest.mark.unit
async def test_work_owed_anyway_runs_after_a_cancellation_that_stops_a_stage() -> None:
    ran: list[str] = []
    runner: list[Any] = []
    started, go = asyncio.Event(), asyncio.Event()

    async def first() -> str:
        started.set()
        await go.wait()
        return "first"

    async def owed() -> str:
        ran.append("owed")
        return "owed"

    async def compose() -> None:
        async with stages() as run:
            runner.append(run)
            await run.stage("workspace", first())
            await run.anyway("collect", owed())

    task = asyncio.create_task(compose())
    await until(started.is_set, task)
    task.cancel()
    await until(lambda: runner[0].cancelled_during is not None, task)
    go.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert ran == ["owed"], "anyway is how a cancelled run still keeps its work"


@pytest.mark.unit
async def test_a_held_cancellation_outranks_an_exception_already_unwinding() -> None:
    runner: list[Any] = []
    started, go = asyncio.Event(), asyncio.Event()

    async def first() -> str:
        started.set()
        await go.wait()
        return "first"

    async def compose() -> None:
        async with stages() as run:
            runner.append(run)
            await run.stage("workspace", first())
            raise ValueError("something else went wrong")

    task = asyncio.create_task(compose())
    await until(started.is_set, task)
    task.cancel()
    await until(lambda: runner[0].cancelled_during is not None, task)
    go.set()
    with pytest.raises(asyncio.CancelledError) as raised:
        await task

    context = raised.value.__context__
    assert isinstance(context, ValueError)
    assert str(context) == "something else went wrong"


@pytest.mark.unit
async def test_a_stage_runner_serves_one_run() -> None:
    run = stages()
    async with run:
        await run.stage("collect", _nothing())

    with pytest.raises(RuntimeError, match="serves one run"):
        async with run:
            pass  # pragma: no cover - entering is what raises


@pytest.mark.unit
async def test_a_stage_outside_the_block_is_an_error() -> None:
    run = stages()
    with pytest.raises(RuntimeError, match="async with stages"):
        await run.stage("collect", _nothing())

    async with run:
        pass
    with pytest.raises(RuntimeError, match="block has ended"):
        await run.stage("collect", _nothing())


@pytest.mark.unit
async def test_the_stage_runner_logs_nothing(
    caplog: pytest.LogCaptureFixture, clean_logging: None
) -> None:
    """Logging is run-level and tied to a run id the runner has no reason to know."""
    with caplog.at_level(logging.DEBUG, logger="waystation"):
        async with stages(Timeouts(collect=30.0)) as run:
            await run.stage("collect", _nothing())
            await run.anyway("sandbox", _nothing(), bound="teardown")

    said = [r.getMessage() for r in caplog.records if r.name.startswith("waystation")]
    assert said == []


@pytest.mark.git
async def test_a_failure_from_host_git_is_attributed_where_the_stage_is_run(
    host_repo: Path,
) -> None:
    """``from_range`` serves a landing and a resolver run, so it names no stage."""
    with pytest.raises(StageError) as alone:
        await PatchSeries.from_range(host_repo, "no-such-base", "HEAD")
    assert alone.value.stage is None, "a helper below a primitive borrows no stage"

    async with stages() as run:
        with pytest.raises(StageError) as staged:
            await run.stage(
                "collect", PatchSeries.from_range(host_repo, "no-such-base", "HEAD")
            )

    assert staged.value.stage == "collect"


@pytest.mark.git
async def test_a_primitive_names_its_own_stage_at_its_edge(host_repo: Path) -> None:
    """``prepare_workspace`` is the workspace stage, so it says so unasked."""
    with pytest.raises(StageError) as raised:
        await prepare_workspace(host_repo, base="no-such-base")

    assert raised.value.stage == "workspace"


@pytest.mark.git
async def test_a_hand_composed_loop_cancelled_mid_agent_keeps_what_the_agent_left(
    host_repo: Path, isolated_tempdir: Path
) -> None:
    """The guarantee that a hand-written loop used to go without entirely."""
    agent = ShellAgent(WORKS_UNTIL_STOPPED)
    ready = asyncio.Event()
    kept: list[str] = []

    def watch(line: AgentLine) -> None:
        if line.raw == "ready":
            ready.set()

    async def compose() -> None:
        async with stages() as run:
            ws = await run.stage("workspace", prepare_workspace(host_repo))
            async with run.entering("sandbox", NoSandbox().start(ws, env={})) as box:
                # Stopped, not waited for: what it committed is still there.
                with contextlib.suppress(asyncio.CancelledError):
                    await run.stage(
                        "agent",
                        run_agent(
                            box,
                            agent,
                            agent.command("work until stopped", {}),
                            Summary,
                            on_output=watch,
                        ),
                        bound=None,
                        interruptible=True,
                    )
                series = await run.anyway("collect", collect(box, ws))
            kept.append(
                await run.anyway(
                    "integrate",
                    preserve_series(
                        host_repo, branch=f"waystation/{ws.run_id}", series=series
                    ),
                    bound=None,
                )
            )

    task = asyncio.create_task(compose())
    await until(ready.is_set, task)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert kept, "the series reached its preservation branch before the cancellation"
    assert subjects(host_repo, f"HEAD..{kept[0]}") == [
        "WIP: salvaged uncommitted work",
        "first",
    ]
    assert branches(host_repo, "waystation/*") == kept
    assert workspaces(isolated_tempdir) == []
