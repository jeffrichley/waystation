"""The live Dashboard hook bundle (issue #35)."""

from __future__ import annotations

import asyncio
import io
import logging
import re
from pathlib import Path

import pytest
from rich.console import Console

from helpers import OK_OUTCOME_LINE, ShellAgent, a_run, awaited, commit_on
from waystation import (
    AgentExit,
    AgentUsage,
    Dashboard,
    Flow,
    GitRepo,
    Integration,
    IntegrationReport,
    NoSandbox,
    PatchSeries,
    RunConflicted,
    RunContext,
    RunFailed,
    RunSucceeded,
    configure_logging,
)
from waystation._run_record import RunRecord
from waystation.agents import AgentLine
from waystation.clock import ManualClock, use_clock


def recording_console() -> Console:
    """A console that keeps what it printed, wide enough that no cell is cut.

    Not a terminal, so a live region draws once, as it closes — what a
    scrollback holds after the batch is done.
    """
    return Console(record=True, file=io.StringIO(), width=160, force_terminal=False)


def table_in(text: str) -> str:
    """The last drawing of the table in ``text``, from its header down."""
    headers = list(re.finditer(r"^ *run +stage +elapsed", text, re.MULTILINE))
    assert headers, f"no dashboard in {text!r}"
    return text[headers[-1].start() :]


def row_of(table: str, label: str) -> str:
    """The one row of ``table`` that carries ``label``."""
    rows = [line for line in table.splitlines() if label in line]
    assert len(rows) == 1, f"expected one row for {label!r}, got {rows!r}"
    return rows[0]


@pytest.mark.git
async def test_a_lone_run_leaves_its_row_beneath_the_log_lines(
    host_repo: Path, clean_logging: None
) -> None:
    console = recording_console()
    configure_logging("INFO", console=console)

    async with Dashboard() as dashboard:
        result = await a_run(host_repo, commits=()).hooks(dashboard)

    assert isinstance(result, RunSucceeded)
    text = console.export_text()
    table = table_in(text)
    row = row_of(table, result.run_id)
    assert "✓" in row, "a finished run shows its result"
    assert "working" in row, "the last thing the agent said"
    assert "collect" in row, "the stage it ended in: it integrated nowhere"
    assert "run end" in text.removesuffix(table), (
        "log lines print above the live region, on the console logging owns"
    )


@pytest.mark.git
async def test_a_batch_keeps_one_row_per_run_after_every_run_ends(
    host_repo: Path, clean_logging: None
) -> None:
    """Awaited together, the way fan-out (#31) will drive them: same hooks."""
    console = recording_console()
    configure_logging("INFO", console=console)
    commit_on(host_repo, "contested", {"a.txt": "theirs\n"})
    dashboard = Dashboard()
    failing = Flow(
        host_repo,
        agent=ShellAgent("echo giving up; exit 3"),
        sandbox=NoSandbox(),
        hooks=[dashboard],
    )

    async with dashboard:
        landed, conflicted, failed = await asyncio.gather(
            awaited(a_run(host_repo).integrate("landing").hooks(dashboard)),
            awaited(a_run(host_repo).integrate("contested").hooks(dashboard)),
            awaited(failing.run("try something")),
        )

    table = table_in(console.export_text())
    assert isinstance(landed, RunSucceeded)
    row = row_of(table, landed.run_id)
    assert "✓" in row
    assert "integrate" in row, "the stage it ended in: it landed"
    assert isinstance(conflicted, RunConflicted)
    row = row_of(table, conflicted.run_id)
    assert "!" in row, "a conflicted run is set apart from a failed one"
    assert "integrate" in row
    assert isinstance(failed, RunFailed)
    row = row_of(table, failed.run_id)
    assert "✗" in row, "a failed run shows it failed"
    assert "agent" in row, "the stage it failed in"
    assert "giving up" in row, "and the last thing its agent said"


def drawn(dashboard: Dashboard) -> str:
    """The dashboard as it stands right now: it is a renderable like any other."""
    console = recording_console()
    console.print(dashboard)
    return console.export_text()


@pytest.mark.git
async def test_a_row_follows_its_run_while_the_run_is_going(host_repo: Path) -> None:
    dashboard = Dashboard()
    midrun: list[str] = []

    def peek(ctx: RunContext, line: AgentLine) -> None:
        midrun.append(row_of(drawn(dashboard), ctx.run_id))

    clock = ManualClock()
    with use_clock(clock):
        result = await (
            a_run(host_repo, commits=())
            .hooks(dashboard)
            .on_sandbox_ready(lambda ctx: clock.advance(65))
            .on_agent_output(peek)
        )

    assert isinstance(result, RunSucceeded)
    row = midrun[0]
    assert "agent" in row, "the stage the run is in now"
    assert "1m05s" in row, "how long it has been going"
    assert "working" in row, "what the agent just said"
    assert not {"✓", "✗", "!"} & set(row), "no result before the run ends"


class _Peeking:
    """An integration strategy that draws the dashboard while it lands."""

    def __init__(self, dashboard: Dashboard, run_ids: list[str]) -> None:
        self.dashboard = dashboard
        self.run_ids = run_ids
        self.midway: list[str] = []

    async def integrate(self, repo: GitRepo, series: PatchSeries) -> IntegrationReport:
        self.midway.append(row_of(drawn(self.dashboard), self.run_ids[0]))
        return await Integration("landing").integrate(repo, series)


@pytest.mark.git
async def test_a_row_shows_a_stage_no_hook_announces(host_repo: Path) -> None:
    """Integrate begins with no hook of its own; the row reads it off ``ctx``."""
    dashboard = Dashboard()
    started: list[str] = []
    peeking = _Peeking(dashboard, started)

    result = await (
        a_run(host_repo)
        .on_run_start(lambda ctx: started.append(ctx.run_id))
        .hooks(dashboard)
        .integrate(peeking)
    )

    assert isinstance(result, RunSucceeded)
    assert "integrate" in peeking.midway[0], "the stage the run is in now"


@pytest.mark.git
async def test_what_an_agent_says_is_shown_as_said_never_as_markup(
    host_repo: Path,
) -> None:
    """``[/]`` closes nothing; read as markup it would take the display down."""
    console = recording_console()
    flow = Flow(
        host_repo,
        agent=ShellAgent(f"echo '[/] [bold]done'; echo '{OK_OUTCOME_LINE}'"),
        sandbox=NoSandbox(),
    )

    async with Dashboard(console) as dashboard:
        result = await flow.run("say something").hooks(dashboard)

    assert isinstance(result, RunSucceeded)
    assert "[/] [bold]done" in row_of(table_in(console.export_text()), result.run_id)


@pytest.mark.git
async def test_a_cancelled_run_is_left_without_a_result_once_the_dashboard_closes(
    host_repo: Path,
) -> None:
    """A cancelled run fires no ``run_end`` (ADR-0017), so nothing else ends it."""
    console = recording_console()
    started: list[str] = []

    def cancel_this_run(ctx: RunContext, line: AgentLine) -> None:
        task = asyncio.current_task()
        assert task is not None
        task.cancel()

    spec = (
        a_run(host_repo, commits=())
        .on_run_start(lambda ctx: started.append(ctx.run_id))
        .on_agent_output(cancel_this_run)
    )
    clock = ManualClock()
    with use_clock(clock):
        async with Dashboard(console) as dashboard:
            task = asyncio.create_task(awaited(spec.hooks(dashboard)))
            with pytest.raises(asyncio.CancelledError):
                await task
        clock.advance(600)
        later = row_of(drawn(dashboard), started[0])

    row = row_of(table_in(console.export_text()), started[0])
    assert "–" in row, "no result, and no longer shown as going"
    assert not {"✓", "✗", "!"} & set(row)
    assert "0.0s" in later, "its clock stopped when the dashboard closed"


class _Unwritable(io.StringIO):
    """A console file that has gone away — stderr piped into ``head``, say."""

    def write(self, text: str) -> int:
        raise OSError("the pipe is closed")


@pytest.mark.git
async def test_a_display_that_cannot_draw_costs_a_log_line_not_the_work(
    host_repo: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Observability that can break the work is worse than none (ADR-0026)."""
    console = Console(file=_Unwritable(), force_terminal=False)

    with caplog.at_level(logging.ERROR, logger="waystation"):
        async with Dashboard(console) as dashboard:
            result = await a_run(host_repo, commits=()).hooks(dashboard)

    assert isinstance(result, RunSucceeded)
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert any("Dashboard" in message for message in errors)


@pytest.mark.unit
def test_a_named_run_shows_its_name_and_what_it_cost(tmp_path: Path) -> None:
    """Driven directly, so the usage is known to the cent."""
    ctx = RunContext(RunRecord(run_id="0badcafe", name="nightly", repo=tmp_path))
    usage = AgentUsage(input_tokens=1200, output_tokens=300, cost_usd=0.4217)
    dashboard = Dashboard()

    dashboard.on_run_start(ctx)
    before = row_of(drawn(dashboard), "nightly")
    dashboard.on_agent_end(
        ctx, AgentExit(exit_code=0, elapsed=4.0, hanging=False, usage=usage)
    )

    row = row_of(drawn(dashboard), "nightly")
    assert "0badcafe" not in row, "a name stands in for the id"
    assert "$0.42" in row, "cost, once the agent reports usage"
    assert "$" not in before, "no cost until there is usage to read it from"
