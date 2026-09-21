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
from collections import defaultdict
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import get_args

import pytest

from helpers import (
    PROMPT,
    WORKS_UNTIL_STOPPED,
    ShellAgent,
    a_run,
    commit_on,
    lifecycle,
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
from waystation.observability import RunEvent

REPO = Path(__file__).resolve().parent.parent
RECORDS_DOC = REPO / "docs" / "log-records.md"


class _Collect(logging.Handler):
    """Files each record's event under the run it belongs to."""

    def __init__(self, events: dict[str, list[str]]) -> None:
        super().__init__()
        self._events = events

    def emit(self, record: logging.LogRecord) -> None:
        run_id = getattr(record, "run_id", None)
        if run_id is not None:
            self._events[run_id].append(getattr(record, "event", ""))


class WatchingTheChannel(HookBundle):
    """A user's bundle that reads the events hooks don't carry.

    Built from the public surface and stdlib logging only — no import from a
    private module — because that is the whole claim being tested: what
    ``RunLogFiles`` sees, a flow script's own bundle can see too. It is the
    recipe ``docs/log-records.md`` documents, kept honest here.

    It joins the channel **when it is constructed**, not per run, and one
    handler serves its whole life, sorting by ``run_id``. A watcher needs no
    per-run machinery — only a file does — and attaching at ``run_start``
    would miss the ``run_start`` record, which the orchestrator logs before
    it fires that hook. The level is given back on the way out, so a bundle
    a script builds and drops does not leave the channel turned up.
    """

    def __init__(self) -> None:
        self.events: dict[str, list[str]] = defaultdict(list)
        self._channel = logging.getLogger("waystation.run")
        # Our own logger's level, so the records exist to be handled: without
        # it the hierarchy sits at the root's WARNING and an INFO line is
        # never made. Turning up one logger the script owns is what
        # per-logger tuning is for, and waystation honours it everywhere.
        self._was = self._channel.level
        self._channel.setLevel(logging.INFO)
        self._handler = _Collect(self.events)
        self._channel.addHandler(self._handler)

    def close(self) -> None:
        self._channel.removeHandler(self._handler)
        self._channel.setLevel(self._was)

    def __enter__(self) -> WatchingTheChannel:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


@dataclass(frozen=True)
class ChattyBackend:
    """A user's sandbox backend that logs a line of its own during a run."""

    said: str
    muttered: str
    inner: NoSandbox = field(default_factory=NoSandbox)

    async def preflight(self) -> None:
        await self.inner.preflight()

    @asynccontextmanager
    async def start(
        self, ws: Workspace, *, env: Mapping[str, str]
    ) -> AsyncIterator[Sandbox]:
        log = run_logger("myco.backend")
        log.info("%s", self.said)
        # DEBUG lands too: `waystation` is held down while a file is open
        # (ADR-0026), and a logger with no level of its own inherits that.
        log.debug("%s", self.muttered)
        async with self.inner.start(ws, env=env) as box:
            yield box


@pytest.mark.git
async def test_a_user_bundle_sees_the_agent_start_that_no_hook_carries(
    host_repo: Path, clean_logging: None
) -> None:
    """The agent stage opens between two hooks; only a record says when."""
    with WatchingTheChannel() as watcher:
        result = await a_run(host_repo).hooks(watcher)

    seen = watcher.events[result.run_id]
    assert "agent_start" in seen, f"the channel carried {seen}"
    # In order, and between the two hooks it sits between.
    assert seen.index("sandbox_ready") < seen.index("agent_start")
    assert seen.index("agent_start") < seen.index("agent_end")


@pytest.mark.git
async def test_a_user_bundle_sees_a_cancellation_with_no_run_file_to_lean_on(
    host_repo: Path, clean_logging: None
) -> None:
    """Alone: no ``RunLogFiles`` holding the hierarchy down on its behalf.

    The bundle has to make the records exist itself, which is the half of
    "as reliably as RunLogFiles" that a test running both would hide.
    """
    with WatchingTheChannel() as watcher:
        await _a_cancelled_run(host_repo)

    (seen,) = watcher.events.values()
    assert "cancelled" in seen, f"the channel carried {seen}"
    assert "run_end" not in seen, "a cancelled run has no result to report"


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
    try:
        async with fan_out([spec, spec]):
            await both.wait()
    finally:
        watcher.close()

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
    muttered = "layer cache was cold"

    with RunLogFiles(logs) as files:
        await a_run(host_repo, sandbox=ChattyBackend(said, muttered)).hooks(files)

    (file,) = logs.glob("*.log")
    written = file.read_text(encoding="utf-8")
    assert said in written
    assert muttered in written, "a run file holds the level, so DEBUG lands too"


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

    events = {getattr(r, "event", None) for r in lifecycle(caplog)}
    assert events == set(_documented_events())


def test_the_documented_events_are_the_ones_the_library_names() -> None:
    """``RunEvent`` is what the table is checked against; keep them one list."""
    assert _documented_events() == list(get_args(RunEvent))


@pytest.mark.git
async def test_every_record_on_the_channel_carries_the_run_and_its_event(
    host_repo: Path, clean_logging: None, caplog: pytest.LogCaptureFixture
) -> None:
    """The extras are the interface too: a bundle filters a fan-out by them."""
    with caplog.at_level(logging.INFO, logger="waystation.run"):
        result = await a_run(host_repo)

    records = lifecycle(caplog)
    assert records, "a run logs its lifecycle"
    for record in records:
        assert record.levelno == logging.INFO
        assert getattr(record, "run_id", None) == result.run_id
        assert getattr(record, "event", None) in get_args(RunEvent)
        # Present and None until a run can be named (#29); the extra is set
        # either way, so a bundle reads one attribute rather than two shapes.
        assert hasattr(record, "run_name")
        assert record.run_name is None


@pytest.mark.git
async def test_only_the_run_channel_marks_its_records_with_an_event(
    host_repo: Path, clean_logging: None, caplog: pytest.LogCaptureFixture
) -> None:
    """The other loggers carry lines, not events, and the doc says so."""
    with caplog.at_level(logging.DEBUG, logger="waystation"):
        await a_run(host_repo)

    elsewhere = [r for r in caplog.records if r.name != "waystation.run"]
    assert elsewhere, "a run logs on its other streams too"
    assert [r.name for r in elsewhere if hasattr(r, "event")] == []


@pytest.mark.git
async def test_every_documented_logger_tags_its_lines_with_the_run(
    host_repo: Path, clean_logging: None, caplog: pytest.LogCaptureFixture
) -> None:
    """The second table's claim: these carry ``run_id`` and ``run_name`` too.

    Only the ones an ordinary run produces are asserted here — ``waystation``
    itself speaks when something went wrong, and ``waystation.agent`` when a
    provider's preflight has a credential to name.
    """
    with caplog.at_level(logging.DEBUG, logger="waystation"):
        result = await a_run(host_repo)

    documented = _documented_loggers()
    seen = {r.name for r in caplog.records}
    assert seen == {
        "waystation.run",
        "waystation.git",
        "waystation.sandbox",
        "waystation.agent.output",
    }, "what an ordinary run speaks on; the rest of the table needs its own run"
    assert seen <= documented, f"logging somewhere undocumented: {seen - documented}"
    for record in caplog.records:
        assert getattr(record, "run_id", None) == result.run_id, record.name
        assert hasattr(record, "run_name"), record.name


def test_run_logger_refuses_a_name_waystation_already_ships() -> None:
    """Records with no ``event`` on the lifecycle channel would break the table."""
    with pytest.raises(ValueError, match="stream waystation ships"):
        run_logger("run")
    with pytest.raises(ValueError, match="puts"):
        run_logger("waystation.myco")
    with pytest.raises(ValueError, match="needs a name"):
        run_logger("")


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
    # The section's first table only: the ones under its ### headings list
    # the extras and the other loggers, which are not events.
    events = re.findall(
        r"^\| `(\w+)` \|", _section("## The run channel"), flags=re.MULTILINE
    )
    assert events, "no event table found under '## The run channel'"
    return events


def _documented_loggers() -> set[str]:
    """Every logger ``docs/log-records.md`` names, the run channel included."""
    loggers = set(
        re.findall(
            r"^\| `(waystation[\w.]*)` \|",
            _section("## The other loggers"),
            flags=re.MULTILINE,
        )
    )
    assert loggers, "no logger table found under '## The other loggers'"
    return loggers | {"waystation.run"}


def _section(heading: str) -> str:
    """The doc from ``heading`` to the next one of its level.

    Parsed rather than duplicated here, so the table a reader is handed is
    the one the assertions run against: an edit to the prose around it is
    free, and an edit to the rows is what fails.
    """
    doc = RECORDS_DOC.read_text(encoding="utf-8")
    assert heading in doc, f"{heading!r} is gone from {RECORDS_DOC.name}"
    return doc.split(heading, 1)[1].split("\n## ", 1)[0].split("\n###", 1)[0]
