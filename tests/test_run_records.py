"""The ``waystation.run`` records as an interface a user's bundle can read (#78).

Two events reach an observer only as log records: the agent starting, and a
run being cancelled. Neither has a hook, by design — the agent stage opens
between ``sandbox_ready`` and the first output line, and a cancelled run has
no result for ``run_end`` (ADR-0017). So the records are the channel, and a
channel nobody can name is not one: these tests hold the names, the levels
and the extras that make it readable, and prove a user's bundle sees the two
events as well as the shipped observer does.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import override

import pytest

from helpers import (
    PROMPT,
    WORKS_UNTIL_STOPPED,
    ShellAgent,
    a_run,
    commit_on,
)
from waystation import (
    Flow,
    HookBundle,
    NoSandbox,
    RunContext,
    RunLogFiles,
    RunSpec,
    Sandbox,
    Summary,
    Workspace,
    fan_out,
    run_logger,
)
from waystation.agents import AgentLine
from waystation.observability import RUN_EVENTS

REPO = Path(__file__).resolve().parent.parent
RECORDS_DOC = REPO / "docs" / "log-records.md"


class _Collect(logging.Handler):
    """Keeps the events one run logged, ignoring every other run's."""

    def __init__(self, run_id: str, seen: list[str]) -> None:
        super().__init__()
        self._run_id = run_id
        self._seen = seen

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(record, "run_id", None) == self._run_id:
            self._seen.append(getattr(record, "event", ""))


class WatchingTheChannel(HookBundle):
    """A user's bundle that reads the events hooks don't carry.

    Built from the public surface and stdlib logging only — no import from a
    private module — because that is the whole claim being tested: what
    ``RunLogFiles`` sees, a flow script's own bundle can see too. It is the
    recipe ``docs/log-records.md`` documents, kept honest here.
    """

    def __init__(self) -> None:
        self.events: dict[str, list[str]] = {}

    @override
    def on_run_start(self, ctx: RunContext) -> None:
        seen: list[str] = []
        self.events[ctx.run_id] = seen
        channel = logging.getLogger("waystation.run")
        # Our own logger's level, so the records exist to be handled: without
        # it the hierarchy sits at the root's WARNING and an INFO line is
        # never made. Tuning one logger is what the channel is read through.
        channel.setLevel(logging.INFO)
        handler = _Collect(ctx.run_id, seen)
        channel.addHandler(handler)

        # A cancelled run fires no run_end, so the task that ran it is what
        # says the watch is over — where RunLogFiles takes it (ADR-0026).
        task = asyncio.current_task()
        if task is not None:
            task.add_done_callback(lambda _: channel.removeHandler(handler))


@dataclass(frozen=True)
class ChattyBackend:
    """A user's sandbox backend that logs a line of its own during a run."""

    said: str
    inner: NoSandbox = field(default_factory=NoSandbox)

    async def preflight(self) -> None:
        await self.inner.preflight()

    @asynccontextmanager
    async def start(
        self, ws: Workspace, *, env: Mapping[str, str]
    ) -> AsyncIterator[Sandbox]:
        run_logger("myco.backend").info("%s", self.said)
        async with self.inner.start(ws, env=env) as box:
            yield box


@pytest.mark.git
async def test_a_user_bundle_sees_the_agent_start_that_no_hook_carries(
    host_repo: Path, clean_logging: None
) -> None:
    """The agent stage opens between two hooks; only a record says when."""
    watcher = WatchingTheChannel()

    result = await a_run(host_repo).hooks(watcher)

    (seen,) = watcher.events.values()
    assert "agent_start" in seen, f"the channel carried {seen}"
    # In order, and between the two hooks it sits between.
    assert seen.index("sandbox_ready") < seen.index("agent_start")
    assert seen.index("agent_start") < seen.index("agent_end")
    assert result.run_id in watcher.events


@pytest.mark.git
async def test_a_user_bundle_sees_a_cancellation_as_a_run_file_does(
    host_repo: Path, tmp_path: Path, clean_logging: None
) -> None:
    """Parity is the claim: what ends the shipped file reaches a user's bundle."""
    logs = tmp_path / "logs"
    watcher = WatchingTheChannel()
    working: set[str] = set()
    both = asyncio.Event()

    def watch(ctx: RunContext, line: AgentLine) -> None:
        if line.raw == "ready":
            working.add(ctx.run_id)
            if len(working) == 2:
                both.set()

    spec: RunSpec[Summary] = (
        Flow(
            host_repo,
            agent=ShellAgent(WORKS_UNTIL_STOPPED),
            sandbox=NoSandbox(),
            hooks=[RunLogFiles(logs), watcher],
        )
        .run("work until stopped")
        .on_agent_output(watch)
    )
    async with fan_out([spec, spec]):
        await both.wait()

    for run_id in working:
        file = (logs / f"{run_id}.log").read_text(encoding="utf-8")
        assert "run cancelled during agent" in file
        assert "cancelled" in watcher.events[run_id], (
            f"RunLogFiles saw the cancellation and the bundle saw "
            f"{watcher.events[run_id]}"
        )


@pytest.mark.git
async def test_a_user_adapters_line_lands_in_the_runs_file(
    host_repo: Path, tmp_path: Path, clean_logging: None
) -> None:
    """A backend of the user's own logs where the shipped ones do."""
    logs = tmp_path / "logs"
    said = "pulled myco/agent:3 for this run"

    with RunLogFiles(logs) as files:
        await a_run(host_repo, sandbox=ChattyBackend(said)).hooks(files)

    (file,) = logs.glob("*.log")
    assert said in file.read_text(encoding="utf-8")


def test_run_logger_puts_a_users_name_on_the_channel() -> None:
    """Under the package logger, which is where a run's handlers are attached."""
    assert run_logger("myco.backend").name == "waystation.myco.backend"


@pytest.mark.git
async def test_the_documented_events_are_the_events_a_run_logs(
    host_repo: Path, clean_logging: None, caplog: pytest.LogCaptureFixture
) -> None:
    """The table is the interface; drift in either direction fails here.

    One run that lands a series and one that is cancelled together reach
    every event the channel has, so a documented event nothing logs and a
    logged event nothing documents are both caught.
    """
    commit_on(host_repo, "agents/x", {"seed.txt": "seed"})
    with caplog.at_level(logging.INFO, logger="waystation.run"):
        await a_run(host_repo).integrate("agents/x")
        await _a_cancelled_run(host_repo)

    records = [r for r in caplog.records if r.name == "waystation.run"]
    assert {getattr(r, "event", None) for r in records} == set(_documented_events())


def test_the_documented_events_are_the_ones_the_library_names() -> None:
    """``RUN_EVENTS`` is what the table is checked against; keep them one list."""
    assert _documented_events() == list(RUN_EVENTS)


@pytest.mark.git
async def test_every_record_on_the_channel_carries_the_run_and_its_event(
    host_repo: Path, clean_logging: None, caplog: pytest.LogCaptureFixture
) -> None:
    """The extras are the interface too: a bundle filters a fan-out by them."""
    with caplog.at_level(logging.INFO, logger="waystation.run"):
        result = await a_run(host_repo)

    records = [r for r in caplog.records if r.name == "waystation.run"]
    assert records, "a run logs its lifecycle"
    for record in records:
        assert record.levelno == logging.INFO
        assert getattr(record, "run_id", None) == result.run_id
        assert getattr(record, "event", None) in RUN_EVENTS
        # Present and None until a run can be named (#29); the extra is set
        # either way, so a bundle reads one attribute rather than two shapes.
        assert hasattr(record, "run_name")
        assert record.run_name is None


@pytest.mark.git
async def test_the_prompt_body_never_reaches_the_channel(
    host_repo: Path, clean_logging: None, caplog: pytest.LogCaptureFixture
) -> None:
    """The agent_start record announces a prompt by shape, and documents that."""
    with caplog.at_level(logging.INFO, logger="waystation.run"):
        await a_run(host_repo)

    (start,) = [r for r in caplog.records if getattr(r, "event", None) == "agent_start"]
    body = PROMPT.splitlines()[1]
    assert body not in start.getMessage()


async def _a_cancelled_run(host_repo: Path) -> None:
    """A run cancelled where it stands, so the channel carries ``cancelled``."""

    def cancel_this_run(ctx: RunContext) -> None:
        task = asyncio.current_task()
        assert task is not None
        task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await a_run(host_repo).on_sandbox_ready(cancel_this_run)


def _documented_events() -> list[str]:
    """The events ``docs/log-records.md`` names, in the order it names them."""
    doc = RECORDS_DOC.read_text(encoding="utf-8")
    # The section's first table only: the ones under its ### headings list
    # the extras and the other loggers, which are not events.
    channel = doc.split("## The run channel", 1)[1].split("\n###", 1)[0]
    return re.findall(r"^\| `(\w+)` \|", channel, flags=re.MULTILINE)
