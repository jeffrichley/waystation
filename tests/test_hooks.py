"""Hooks: lifecycle points, registration and RunContext (issue #28)."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, override

import pytest
from pydantic import BaseModel

from helpers import OK_OUTCOME_LINE, OUTCOME, ShellAgent, commit_on, git
from waystation import (
    AgentExit,
    AgentExited,
    Flow,
    HookBundle,
    HookRaised,
    Integration,
    IntegrationReport,
    NoSandbox,
    RunConflicted,
    RunContext,
    RunFailed,
    RunResult,
    RunSucceeded,
    ScriptedAgent,
    ScriptedCommit,
    TimedOut,
    Timeouts,
)
from waystation.agents import (
    AgentLine,
    AgentText,
    OutcomeReported,
)
from waystation.clock import ManualClock, use_clock


class Answer(BaseModel):
    summary: str


async def elapse(clock: ManualClock, seconds: float) -> None:
    """Let armed timers park on ``clock``, pass ``seconds``, and let them react."""
    for _ in range(5):
        await asyncio.sleep(0)
    clock.advance(seconds)
    for _ in range(5):
        await asyncio.sleep(0)


def committed_then_reports(message: str) -> ShellAgent:
    """Commit silently, then print a line and a valid Outcome.

    Git finishes before the first output line, so a hook that stops the agent
    on that line never kills git mid-write.
    """
    return ShellAgent(
        " && ".join(
            [
                f"printf x > '{message}.txt'",
                "git add -A >/dev/null 2>&1",
                f"git commit -qm '{message}' >/dev/null 2>&1",
                "echo working",
                f"echo '{OK_OUTCOME_LINE}'",
            ]
        )
    )


class Recorder:
    """A bundle that records every call it receives."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, RunContext, Any]] = []

    @property
    def names(self) -> list[str]:
        return [name for name, _, _ in self.calls]

    def args(self, hook: str) -> list[Any]:
        return [arg for name, _, arg in self.calls if name == hook]

    def on_run_start(self, ctx: RunContext) -> None:
        self.calls.append(("run_start", ctx, None))

    def on_workspace_ready(self, ctx: RunContext) -> None:
        self.calls.append(("workspace_ready", ctx, None))

    def on_sandbox_ready(self, ctx: RunContext) -> None:
        self.calls.append(("sandbox_ready", ctx, None))

    def on_agent_output(self, ctx: RunContext, line: AgentLine) -> None:
        self.calls.append(("agent_output", ctx, line))

    def on_agent_end(self, ctx: RunContext, exit: AgentExit) -> None:
        self.calls.append(("agent_end", ctx, exit))

    def on_integrated(self, ctx: RunContext, report: IntegrationReport) -> None:
        self.calls.append(("integrated", ctx, report))

    def on_run_end(self, ctx: RunContext, result: Any) -> None:
        self.calls.append(("run_end", ctx, result))


def _collapse(names: list[str]) -> list[str]:
    """Fold consecutive repeats (one entry per run of agent_output lines)."""
    return [n for i, n in enumerate(names) if i == 0 or names[i - 1] != n]


@pytest.mark.git
@pytest.mark.asyncio
async def test_bundle_sees_run_start_and_run_end_with_result(
    host_repo: Path,
) -> None:
    recorder = Recorder()
    flow = Flow(
        host_repo,
        agent=ScriptedAgent(outcome=Answer(summary="ok")),
        sandbox=NoSandbox(),
        hooks=[recorder],
    )

    result = await flow.run("hello", outcome=Answer)

    assert isinstance(result, RunSucceeded)
    assert recorder.names[0] == "run_start"
    assert recorder.names[-1] == "run_end"
    assert all(ctx.run_id == result.run_id for _, ctx, _ in recorder.calls)
    assert recorder.args("run_end") == [result]


@pytest.mark.git
@pytest.mark.asyncio
async def test_hook_points_fire_in_lifecycle_order(host_repo: Path) -> None:
    recorder = Recorder()
    flow = Flow(
        host_repo,
        agent=ScriptedAgent(
            lines=["working"],
            commits=(ScriptedCommit(message="add feat", files={"feat.txt": "x\n"}),),
            outcome=Answer(summary="ok"),
        ),
        sandbox=NoSandbox(),
        integration=Integration("agents/hooks"),
        hooks=[recorder],
    )

    result = await flow.run("land it", outcome=Answer)

    assert isinstance(result, RunSucceeded)
    assert _collapse(recorder.names) == [
        "run_start",
        "workspace_ready",
        "sandbox_ready",
        "agent_output",
        "agent_end",
        "integrated",
        "run_end",
    ]
    assert recorder.args("agent_end") == [result.agent]
    assert recorder.args("integrated") == [result.report]
    assert recorder.args("run_end") == [result]


@pytest.mark.git
@pytest.mark.asyncio
async def test_agent_output_carries_stream_raw_and_events(host_repo: Path) -> None:
    recorder = Recorder()
    script = "; ".join(
        [
            "echo hello",
            "echo warned >&2",
            f"echo '{OK_OUTCOME_LINE}'",
        ]
    )
    flow = Flow(
        host_repo,
        agent=ShellAgent(script),
        sandbox=NoSandbox(),
        hooks=[recorder],
    )

    result = await flow.run("talk", outcome=Answer)

    assert isinstance(result, RunSucceeded)
    lines: list[AgentLine] = recorder.args("agent_output")
    assert [line for line in lines if line.stream == "stdout"] == [
        AgentLine("stdout", "hello", (AgentText("hello"),)),
        AgentLine("stdout", OK_OUTCOME_LINE, (OutcomeReported({"summary": "ok"}),)),
    ]
    assert [line for line in lines if line.stream == "stderr"] == [
        AgentLine("stderr", "warned", ()),
    ]


class Labelled:
    """A bundle that appends its label to a shared list at run_start."""

    def __init__(self, label: str, seen: list[str]) -> None:
        self.label = label
        self.seen = seen

    def on_run_start(self, ctx: RunContext) -> None:
        self.seen.append(self.label)


@pytest.mark.git
@pytest.mark.asyncio
async def test_flow_hooks_fire_before_per_run_hooks_and_none_shadow(
    host_repo: Path,
) -> None:
    seen: list[str] = []
    flow = Flow(
        host_repo,
        agent=ScriptedAgent(outcome=Answer(summary="ok")),
        sandbox=NoSandbox(),
        hooks=[Labelled("flow bundle", seen)],
    )

    def flow_function(ctx: RunContext) -> None:
        seen.append("flow decorator")

    assert flow.on_run_start(flow_function) is flow_function
    plain = flow.run("hello", outcome=Answer)
    hooked = plain.hooks(Labelled("run bundle", seen)).on_run_start(
        lambda ctx: seen.append("run function")
    )

    await hooked
    assert seen == ["flow bundle", "flow decorator", "run bundle", "run function"]

    seen.clear()
    await plain
    assert seen == ["flow bundle", "flow decorator"]


@pytest.mark.git
@pytest.mark.asyncio
async def test_every_hook_point_registers_by_decorator_and_per_run(
    host_repo: Path,
) -> None:
    calls: list[tuple[str, str]] = []

    def record(level: str, hook: str) -> Any:
        return lambda ctx, *args: calls.append((hook, level))

    flow = Flow(
        host_repo,
        agent=ScriptedAgent(
            commits=(ScriptedCommit(message="add feat", files={"feat.txt": "x\n"}),),
            outcome=Answer(summary="ok"),
        ),
        sandbox=NoSandbox(),
        integration=Integration("agents/every-hook"),
    )
    flow.on_run_start(record("flow", "run_start"))
    flow.on_workspace_ready(record("flow", "workspace_ready"))
    flow.on_sandbox_ready(record("flow", "sandbox_ready"))
    flow.on_agent_output(record("flow", "agent_output"))
    flow.on_agent_end(record("flow", "agent_end"))
    flow.on_integrated(record("flow", "integrated"))
    flow.on_run_end(record("flow", "run_end"))
    spec = (
        flow.run("land it", outcome=Answer)
        .on_run_start(record("run", "run_start"))
        .on_workspace_ready(record("run", "workspace_ready"))
        .on_sandbox_ready(record("run", "sandbox_ready"))
        .on_agent_output(record("run", "agent_output"))
        .on_agent_end(record("run", "agent_end"))
        .on_integrated(record("run", "integrated"))
        .on_run_end(record("run", "run_end"))
    )

    result = await spec

    assert isinstance(result, RunSucceeded)
    for hook in (
        "run_start",
        "workspace_ready",
        "sandbox_ready",
        "agent_output",
        "agent_end",
        "integrated",
        "run_end",
    ):
        levels = [level for name, level in calls if name == hook]
        assert levels, hook
        assert levels == ["flow", "run"] * (len(levels) // 2), hook


@pytest.mark.git
@pytest.mark.asyncio
async def test_run_spec_snapshots_flow_hooks_at_flow_run(host_repo: Path) -> None:
    seen: list[str] = []
    flow = Flow(
        host_repo,
        agent=ScriptedAgent(outcome=Answer(summary="ok")),
        sandbox=NoSandbox(),
    )
    early = flow.run("before", outcome=Answer)

    @flow.on_run_start
    def late(ctx: RunContext) -> None:
        seen.append(ctx.run_id)

    await early
    assert seen == []

    result = await flow.run("after", outcome=Answer)
    assert seen == [result.run_id]


@pytest.mark.git
@pytest.mark.asyncio
async def test_sync_and_async_hooks_run_one_at_a_time(host_repo: Path) -> None:
    seen: list[str] = []
    flow = Flow(
        host_repo,
        agent=ScriptedAgent(outcome=Answer(summary="ok")),
        sandbox=NoSandbox(),
    )

    @flow.on_sandbox_ready
    async def slow(ctx: RunContext) -> None:
        seen.append("async begins")
        for _ in range(5):
            await asyncio.sleep(0.01)
        seen.append("async ends")

    @flow.on_sandbox_ready
    def quick(ctx: RunContext) -> None:
        seen.append("sync")

    result = await flow.run("mixed", outcome=Answer)

    assert isinstance(result, RunSucceeded)
    assert seen == ["async begins", "async ends", "sync"]


@pytest.mark.git
@pytest.mark.asyncio
async def test_run_context_is_read_only_and_sandbox_is_scoped(
    host_repo: Path,
) -> None:
    facts: dict[str, Any] = {}

    def sandbox_usable(ctx: RunContext) -> bool:
        try:
            ctx.sandbox  # noqa: B018
        except RuntimeError:
            return False
        return True

    class Probe:
        def on_run_start(self, ctx: RunContext) -> None:
            facts["start"] = (ctx.base_sha, sandbox_usable(ctx))
            with pytest.raises(AttributeError):
                ctx.run_id = "forged"  # type: ignore[misc]

        def on_workspace_ready(self, ctx: RunContext) -> None:
            facts["workspace"] = (ctx.base_sha, sandbox_usable(ctx))

        async def on_sandbox_ready(self, ctx: RunContext) -> None:
            probe = await ctx.sandbox.exec(["git", "rev-parse", "HEAD"])
            facts["sandbox"] = (ctx.base_sha, probe.stdout.strip())

        def on_integrated(self, ctx: RunContext, report: IntegrationReport) -> None:
            facts["integrated"] = sandbox_usable(ctx)

        def on_run_end(self, ctx: RunContext, result: Any) -> None:
            facts["end"] = (ctx.run_id, ctx.name, ctx.repo, sandbox_usable(ctx))

    flow = Flow(
        host_repo,
        agent=ScriptedAgent(outcome=Answer(summary="ok")),
        sandbox=NoSandbox(),
        integration=Integration("agents/probe"),
        hooks=[Probe()],
    )

    result = await flow.run("probe", outcome=Answer)

    assert isinstance(result, RunSucceeded)
    head = git(host_repo, "rev-parse", "HEAD")
    assert result.base_sha == head
    assert facts == {
        "start": (None, False),
        "workspace": (head, False),
        "sandbox": (head, head),
        "integrated": False,
        "end": (result.run_id, None, host_repo, False),
    }


@pytest.mark.git
@pytest.mark.asyncio
async def test_sandbox_ready_hook_sets_up_what_the_agent_sees(
    host_repo: Path,
) -> None:
    recorder = Recorder()
    flow = Flow(
        host_repo,
        agent=ShellAgent(f"printf '{OUTCOME}'; cat setup.json; echo"),
        sandbox=NoSandbox(),
        hooks=[recorder],
    )

    @flow.on_sandbox_ready
    async def setup(ctx: RunContext) -> None:
        done = await ctx.sandbox.exec(
            [
                *ctx.sandbox.shell,
                'echo setup-noise; printf "%s" "$0" > setup.json',
                '{"summary": "set up"}',
            ]
        )
        assert done.exit_code == 0

    result = await flow.run("use the setup", outcome=Answer)

    assert isinstance(result, RunSucceeded)
    assert result.outcome == Answer(summary="set up")
    raw_lines = [line.raw for line in recorder.args("agent_output")]
    assert raw_lines
    assert "setup-noise" not in raw_lines


@pytest.mark.git
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("hook", "stage"),
    [
        ("run_start", "workspace"),
        ("workspace_ready", "workspace"),
        ("sandbox_ready", "sandbox"),
        ("agent_output", "agent"),
        ("agent_end", "agent"),
        ("integrated", "integrate"),
        ("run_end", "integrate"),
    ],
)
async def test_raising_hook_fails_run_at_the_stage_whose_boundary_fired(
    host_repo: Path, hook: str, stage: str
) -> None:
    recorder = Recorder()
    error = ValueError(f"broken {hook} hook")
    flow = Flow(
        host_repo,
        agent=committed_then_reports("add feat"),
        sandbox=NoSandbox(),
        integration=Integration("agents/raising"),
        hooks=[recorder],
    )

    def boom(ctx: RunContext, *args: Any) -> None:
        raise error

    spec = flow.run("raise", outcome=Answer)
    result = await getattr(spec, f"on_{hook}")(boom)

    assert isinstance(result, RunFailed)
    assert result.stage == stage
    assert isinstance(result.failure, HookRaised)
    assert result.failure.hook == hook
    assert result.failure.function.endswith("boom")
    assert result.failure.exception is error
    if hook != "run_end":
        assert recorder.args("run_end") == [result]


@pytest.mark.git
@pytest.mark.asyncio
@pytest.mark.parametrize("hook", ["agent_output", "agent_end"])
async def test_raising_hook_past_agent_start_preserves_the_series(
    host_repo: Path, hook: str
) -> None:
    flow = Flow(
        host_repo,
        agent=committed_then_reports("keep me"),
        sandbox=NoSandbox(),
    )

    def boom(ctx: RunContext, arg: Any) -> None:
        raise RuntimeError("hook bug")

    result = await getattr(flow.run("keep", outcome=Answer), f"on_{hook}")(boom)

    assert isinstance(result, RunFailed)
    assert result.stage == "agent"
    assert isinstance(result.failure, HookRaised)
    assert result.preserved == f"waystation/{result.run_id}"
    assert git(host_repo, "log", "-1", "--format=%s", result.preserved) == "keep me"


@pytest.mark.git
@pytest.mark.asyncio
async def test_raising_agent_output_hook_stops_the_agent(host_repo: Path) -> None:
    flow = Flow(
        host_repo,
        agent=ShellAgent("echo first; sleep 30; echo never"),
        sandbox=NoSandbox(),
    )
    seen: list[str] = []

    def boom(ctx: RunContext, line: AgentLine) -> None:
        seen.append(line.raw)
        raise RuntimeError("stop")

    result = await flow.run("stop", outcome=Answer).on_agent_output(boom)

    assert isinstance(result, RunFailed)
    assert isinstance(result.failure, HookRaised)
    assert seen == ["first"]
    assert result.elapsed["agent"] < 15


@pytest.mark.git
@pytest.mark.asyncio
async def test_raising_integrated_hook_keeps_landed_series_unpreserved(
    host_repo: Path,
) -> None:
    flow = Flow(
        host_repo,
        agent=ScriptedAgent(
            commits=(ScriptedCommit(message="landed", files={"l.txt": "x\n"}),),
            outcome=Answer(summary="ok"),
        ),
        sandbox=NoSandbox(),
        integration=Integration("agents/landed"),
    )

    def boom(ctx: RunContext, report: IntegrationReport) -> None:
        raise RuntimeError("after landing")

    result = await flow.run("land", outcome=Answer).on_integrated(boom)

    assert isinstance(result, RunFailed)
    assert result.stage == "integrate"
    assert result.preserved is None
    assert git(host_repo, "log", "-1", "--format=%s", "agents/landed") == "landed"
    refs = git(host_repo, "for-each-ref", "--format=%(refname:short)").splitlines()
    assert f"waystation/{result.run_id}" not in refs


@pytest.mark.git
@pytest.mark.asyncio
async def test_integrated_fires_only_when_integration_lands(host_repo: Path) -> None:
    commit_on(host_repo, "agents/taken", {"clash.txt": "theirs\n"})
    agent = ScriptedAgent(
        commits=(ScriptedCommit(message="ours", files={"clash.txt": "ours\n"}),),
        outcome=Answer(summary="ok"),
    )
    no_integration = Recorder()
    conflicting = Recorder()
    flow = Flow(host_repo, agent=agent, sandbox=NoSandbox())

    unintegrated = await flow.run("keep", outcome=Answer).hooks(no_integration)
    collided = (
        await flow.run("collide", outcome=Answer)
        .integrate("agents/taken")
        .hooks(conflicting)
    )

    assert isinstance(unintegrated, RunSucceeded)
    assert "integrated" not in no_integration.names
    assert no_integration.args("run_end") == [unintegrated]
    assert isinstance(collided, RunConflicted)
    assert "integrated" not in conflicting.names
    assert conflicting.args("run_end") == [collided]


@pytest.mark.git
@pytest.mark.asyncio
async def test_failures_ride_run_end_with_no_failed_or_conflicted_hook(
    host_repo: Path,
) -> None:
    class Hopeful:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def on_failed(self, ctx: RunContext, *args: Any) -> None:
            self.calls.append("failed")

        def on_conflicted(self, ctx: RunContext, *args: Any) -> None:
            self.calls.append("conflicted")

        def on_agent_end(self, ctx: RunContext, exit: AgentExit) -> None:
            self.calls.append(f"agent_end {exit.exit_code}")

        def on_run_end(self, ctx: RunContext, result: Any) -> None:
            self.calls.append(type(result).__name__)

    hopeful = Hopeful()
    flow = Flow(
        host_repo,
        agent=ScriptedAgent(outcome=Answer(summary="ok"), exit_code=3),
        sandbox=NoSandbox(),
        hooks=[hopeful],
    )

    result = await flow.run("fail", outcome=Answer)

    assert isinstance(result, RunFailed)
    assert hopeful.calls == ["agent_end 3", "RunFailed"]
    for owner in (flow, flow.run("fail", outcome=Answer)):
        assert not hasattr(owner, "on_failed")
        assert not hasattr(owner, "on_conflicted")


@pytest.mark.git
def test_a_bundle_without_hook_methods_is_rejected(host_repo: Path) -> None:
    def not_a_bundle(ctx: RunContext) -> None:
        return None

    with pytest.raises(TypeError, match="on_run_start"):
        Flow(
            host_repo,
            agent=ScriptedAgent(),
            sandbox=NoSandbox(),
            hooks=[not_a_bundle],
        )
    flow = Flow(host_repo, agent=ScriptedAgent(), sandbox=NoSandbox())
    with pytest.raises(TypeError, match="on_run_start"):
        flow.run("x").hooks(object())


@pytest.mark.git
@pytest.mark.parametrize("stream", ["stdout", "stderr"])
async def test_an_output_line_of_any_length_reaches_on_agent_output_intact(
    host_repo: Path, stream: str
) -> None:
    # One JSON event from `claude -p --output-format stream-json` carrying a
    # large tool result is one line, well past asyncio's 64 KiB line limit.
    size = 200_000
    redirect = " >&2" if stream == "stderr" else ""
    seen: list[AgentLine] = []

    def record(ctx: RunContext, line: AgentLine) -> None:
        seen.append(line)

    flow = Flow(
        host_repo,
        agent=ShellAgent(
            f"{{ head -c {size} /dev/zero | tr '\\0' x; echo; }}{redirect}; "
            f"echo '{OK_OUTCOME_LINE}'"
        ),
        sandbox=NoSandbox(),
    )

    result = await flow.run("long line", outcome=Answer).on_agent_output(record)

    assert isinstance(result, RunSucceeded)
    assert "x" * size in [line.raw for line in seen if line.stream == stream]


@pytest.mark.git
@pytest.mark.parametrize("stream", ["stdout", "stderr"])
async def test_an_output_line_that_is_not_utf8_reaches_on_agent_output_readable(
    host_repo: Path, stream: str
) -> None:
    # Only captured output keeps such a byte exact, for git. A hook reads a
    # line as a person would, so it gets U+FFFD, never a lone surrogate it
    # could not print. printf makes the byte: a raw one in a Windows command
    # line may not reach Git Bash's sh.
    redirect = " >&2" if stream == "stderr" else ""
    seen: list[AgentLine] = []

    def record(ctx: RunContext, line: AgentLine) -> None:
        seen.append(line)

    flow = Flow(
        host_repo,
        agent=ShellAgent(f"printf 'caf\\351\\n'{redirect}; echo '{OK_OUTCOME_LINE}'"),
        sandbox=NoSandbox(),
    )

    result = await flow.run("latin-1", outcome=Answer).on_agent_output(record)

    assert isinstance(result, RunSucceeded)
    lines = [line.raw for line in seen if line.stream == stream]
    assert lines[0] == "caf\N{REPLACEMENT CHARACTER}"


@pytest.mark.git
@pytest.mark.asyncio
async def test_agent_output_hooks_never_overlap_across_streams(
    host_repo: Path,
) -> None:
    active = 0
    peak = 0

    async def slow(ctx: RunContext, line: AgentLine) -> None:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.05)
        active -= 1

    flow = Flow(
        host_repo,
        agent=ShellAgent(
            "; ".join(
                [
                    "echo out-1",
                    "echo err-1 >&2",
                    "echo out-2",
                    "echo err-2 >&2",
                    f"echo '{OK_OUTCOME_LINE}'",
                ]
            )
        ),
        sandbox=NoSandbox(),
    )

    result = await flow.run("both streams", outcome=Answer).on_agent_output(slow)

    assert isinstance(result, RunSucceeded)
    assert peak == 1


@pytest.mark.git
@pytest.mark.asyncio
async def test_time_spent_in_agent_output_hooks_is_not_agent_silence(
    host_repo: Path,
) -> None:
    clock = ManualClock()

    async def slower_than_the_bound(ctx: RunContext, line: AgentLine) -> None:
        await elapse(clock, 60)

    flow = Flow(
        host_repo,
        agent=ShellAgent(f"echo first; echo second; echo '{OK_OUTCOME_LINE}'"),
        sandbox=NoSandbox(),
        timeouts=Timeouts(agent_silence=5, completion_grace=5),
    )

    with use_clock(clock):
        result = await flow.run("slow hooks", outcome=Answer).on_agent_output(
            slower_than_the_bound
        )

    assert isinstance(result, RunSucceeded)
    assert result.agent is not None
    assert result.agent.hanging is False


@pytest.mark.git
@pytest.mark.asyncio
async def test_every_run_end_hook_fires_even_after_one_raises(
    host_repo: Path, caplog: pytest.LogCaptureFixture
) -> None:
    seen: list[Any] = []
    error = RuntimeError("run_end bug")

    def boom(ctx: RunContext, result: Any) -> None:
        raise error

    def records_then_raises(ctx: RunContext, result: Any) -> None:
        seen.append(result)
        raise RuntimeError("second run_end bug")

    flow = Flow(
        host_repo,
        agent=ScriptedAgent(outcome=Answer(summary="ok")),
        sandbox=NoSandbox(),
    )

    with caplog.at_level(logging.ERROR, logger="waystation"):
        result = await (
            flow.run("end", outcome=Answer)
            .on_run_end(boom)
            .on_run_end(records_then_raises)
        )

    assert isinstance(result, RunFailed)
    assert isinstance(result.failure, HookRaised)
    assert result.failure.exception is error
    assert len(seen) == 1
    assert isinstance(seen[0], RunSucceeded)
    assert seen[0].run_id == result.run_id
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "second run_end bug" in logged


@pytest.mark.git
@pytest.mark.asyncio
async def test_agent_end_fires_when_the_agent_is_stopped(host_repo: Path) -> None:
    clock = ManualClock()
    recorder = Recorder()

    async def past_the_wall(ctx: RunContext, line: AgentLine) -> None:
        await elapse(clock, 60)

    flow = Flow(
        host_repo,
        agent=ShellAgent("echo tick; sleep 30"),
        sandbox=NoSandbox(),
        timeouts=Timeouts(agent_wall=5),
        hooks=[recorder],
    )

    with use_clock(clock):
        result = await flow.run("stopped", outcome=Answer).on_agent_output(
            past_the_wall
        )

    assert isinstance(result, RunFailed)
    assert isinstance(result.failure, TimedOut)
    assert result.failure.bound == "agent_wall"
    assert result.agent is not None
    assert result.agent.exit_code == -1
    assert recorder.args("agent_end") == [result.agent]


@pytest.mark.git
@pytest.mark.asyncio
async def test_hook_raising_after_a_failure_is_logged_not_reported(
    host_repo: Path, caplog: pytest.LogCaptureFixture
) -> None:
    flow = Flow(
        host_repo,
        agent=ScriptedAgent(outcome=Answer(summary="ok"), exit_code=3),
        sandbox=NoSandbox(),
    )

    def broken_end(ctx: RunContext, exit: AgentExit) -> None:
        raise RuntimeError("agent_end bug")

    def broken_run_end(ctx: RunContext, result: Any) -> None:
        raise RuntimeError("run_end bug")

    spec = flow.run("fail", outcome=Answer)
    with caplog.at_level(logging.ERROR, logger="waystation"):
        result = await spec.on_agent_end(broken_end).on_run_end(broken_run_end)

    assert isinstance(result, RunFailed)
    assert result.stage == "agent"
    assert isinstance(result.failure, AgentExited)
    assert result.failure.exit_code == 3
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "agent_end bug" in logged
    assert "run_end bug" in logged


class EndOnly(HookBundle):
    """A bundle that overrides one hook and inherits no-ops for the rest."""

    def __init__(self) -> None:
        self.results: list[RunResult[Any]] = []

    @override
    async def on_run_end(
        self,
        ctx: RunContext,
        result: RunResult[Any],
    ) -> None:
        self.results.append(result)


@pytest.mark.git
@pytest.mark.asyncio
async def test_hook_bundle_subclass_overrides_only_what_it_needs(
    host_repo: Path,
) -> None:
    bundle = EndOnly()
    flow = Flow(
        host_repo,
        agent=ScriptedAgent(
            commits=(ScriptedCommit(message="feat", files={"f.txt": "x\n"}),),
            outcome=Answer(summary="ok"),
        ),
        sandbox=NoSandbox(),
        integration=Integration("agents/bundle"),
        hooks=[bundle, HookBundle()],
    )

    result = await flow.run("bundle", outcome=Answer)

    assert isinstance(result, RunSucceeded)
    assert bundle.results == [result]
