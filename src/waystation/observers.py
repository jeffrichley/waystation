"""Built-in observers: the hook bundles waystation ships (ADR-0001).

Every observer here is an ordinary bundle — an object with ``on_<hook>``
methods, the same shape a user's own bundle has — so nothing the library
watches with is privileged over what a flow script watches with.

``RunLog`` is the one the orchestrator always carries. It is called directly
rather than registered, so its lines fire in a fixed order and still fire
when a user hook raises; being a bundle is what keeps its vocabulary and the
hook vocabulary the same one.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from typing import Any, Concatenate, NamedTuple, cast, override

from pydantic_core import to_jsonable_python
from rich import box
from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.text import Text

from waystation.agents.protocol import AgentLine, AgentText
from waystation.clock import Clock, get_clock
from waystation.hooks import HookBundle, HookName, RunContext
from waystation.observability import (
    AGENT_OUTPUT,
    PACKAGE,
    RUN,
    configured_console,
    tag,
    tagged_logger,
)
from waystation.results import (
    AgentExit,
    IntegrationReport,
    RunConflicted,
    RunFailed,
    RunResult,
    RunSucceeded,
    Stage,
    TimedOut,
)

__all__ = ["Dashboard", "EventLog", "RunLog", "RunLogFiles"]

logger = tagged_logger(PACKAGE)

# How much of a prompt's first line an INFO line may carry. The rest of the
# prompt never reaches a log record at all.
PROMPT_HEAD = 80


def _head(prompt: str) -> str:
    """The prompt's first line, bounded; never more of it than that."""
    first = prompt.splitlines()[0] if prompt else ""
    return first if len(first) <= PROMPT_HEAD else f"{first[: PROMPT_HEAD - 1]}…"


class RunLog(HookBundle):
    """One run's lifecycle on the console: an INFO line per event, DEBUG output.

    Both loggers are bound to the run, so a line carries its run even when it
    is logged from a task the run's own context never reached.
    """

    __slots__ = ("_output", "_run")

    def __init__(self, run_id: str, name: str | None) -> None:
        self._run = tag(RUN, run_id, name)
        self._output = tag(AGENT_OUTPUT, run_id, name)

    @override
    def on_run_start(self, ctx: RunContext) -> None:
        self._run.info("run start: %s", ctx.repo)

    @override
    def on_workspace_ready(self, ctx: RunContext) -> None:
        self._run.info("workspace ready: base %s", (ctx.base_sha or "")[:12])

    @override
    def on_sandbox_ready(self, ctx: RunContext) -> None:
        self._run.info("sandbox up")

    @override
    def on_agent_output(self, ctx: RunContext, line: AgentLine) -> None:
        prefix = "[stderr] " if line.stream == "stderr" else ""
        self._output.debug("%s%s", prefix, line.raw)

    @override
    def on_agent_end(self, ctx: RunContext, exit: AgentExit) -> None:
        self._run.info("agent end: exit %d in %.1fs", exit.exit_code, exit.elapsed)

    @override
    def on_integrated(self, ctx: RunContext, report: IntegrationReport) -> None:
        landed = len(report.landed)
        self._run.info(
            "integrated: %d commit%s onto %s",
            landed,
            "" if landed == 1 else "s",
            report.target,
        )

    @override
    def on_run_end(
        self,
        ctx: RunContext,
        result: RunResult[Any],
    ) -> None:
        elapsed = sum(result.elapsed.values())
        if isinstance(result, RunFailed):
            self._run.info(
                "run end: failed at %s (%s) in %.1fs",
                result.stage,
                type(result.failure).__name__,
                elapsed,
            )
            return
        if isinstance(result, RunConflicted):
            self._run.info(
                "run end: conflicted landing on %s in %.1fs; series kept on %s",
                result.report.target,
                elapsed,
                result.preserved,
            )
            return
        self._run.info("run end: succeeded in %.1fs", elapsed)

    # The agent's start and a run's cancellation are the events with no hook
    # behind them: the agent stage opens between sandbox_ready and the first
    # output line, and a cancelled run has no result for run_end (ADR-0017).
    def agent_start(self, prompt: str) -> None:
        """Announce the prompt by shape only — its body is never logged."""
        self._run.info(
            "agent start: prompt %d chars, first line %r", len(prompt), _head(prompt)
        )

    def cancelled(
        self, stage: Stage, *, kept_on: str | None, landed_on: str | None
    ) -> None:
        """Say a run was cancelled, and where its series went, if anywhere."""
        if kept_on is not None:
            self._run.info("run cancelled during %s; series kept on %s", stage, kept_on)
        elif landed_on is not None:
            self._run.info(
                "run cancelled during %s; series landed on %s", stage, landed_on
            )
        else:
            self._run.info("run cancelled during %s", stage)


class _FlushingFile:
    """An append-only text file whose every line is flushed as it lands.

    Both observers want the same thing: a reader — ``tail -f``, or an analysis
    script watching a fan-out — should see a line the moment it happens, not
    when the run ends. Writes come from whichever thread logged — a hook may
    log from a worker thread while agent lines arrive on the event loop — so
    they go through a lock and no line lands inside another.
    """

    __slots__ = ("_handle", "_lock", "path")

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = path.open("a", encoding="utf-8", errors="replace")
        self._lock = threading.Lock()

    def write(self, text: str) -> None:
        with self._lock:
            self._handle.write(text if text.endswith("\n") else f"{text}\n")
            self._handle.flush()

    def close(self) -> None:
        with self._lock:
            self._handle.close()


class _OpenRunFile(NamedTuple):
    """A run's file, the handler feeding it, and the watch on the task running it.

    They open and close together. ``task`` is ``None`` for a hook called by
    hand, with no run around it.
    """

    file: _FlushingFile
    handler: _RunFileHandler
    task: asyncio.Task[Any] | None
    on_task_done: Callable[[asyncio.Task[Any]], None]


def _running_task() -> asyncio.Task[Any] | None:
    """The task a hook is running in, or ``None`` outside an event loop."""
    try:
        return asyncio.current_task()
    except RuntimeError:  # no running loop: a hook called by hand
        return None


class _RunFileHandler(logging.Handler):
    """Routes one run's records into its file, leaving other runs alone."""

    def __init__(self, run_id: str, file: _FlushingFile) -> None:
        super().__init__(logging.DEBUG)
        self._run_id = run_id
        self._file = file

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(record, "run_id", None) != self._run_id:
            return
        # The agent's own lines are written raw by on_agent_output; taking them
        # from the logger too would double every one of them.
        if record.name == AGENT_OUTPUT.name:
            return
        try:
            self._file.write(self.format(record))
        except Exception:  # pragma: no cover - logging swallows its own errors
            self.handleError(record)


def _inherited_level() -> int:
    """The level the ``waystation`` logger takes from above, ignoring its own.

    What a host application's configuration says waystation may log — read
    live, so a host that turns its own level down mid-run is still obeyed.
    """
    logger = logging.getLogger(PACKAGE).parent
    while logger is not None:
        if logger.level:
            return logger.level
        logger = logger.parent
    return logging.NOTSET


class _RelayAbove(logging.Handler):
    """Hands records up the hierarchy, at the level it would have seen.

    While a run file holds the ``waystation`` logger at DEBUG, the logger
    stops propagating, so the extra records it created reach nothing else.
    This puts back exactly what was already going up: no more, and nothing
    that was arriving before is lost (ADR-0026).
    """

    def __init__(self, held_level: int) -> None:
        super().__init__(logging.NOTSET)
        # The logger's own level before it was raised. NOTSET means it had
        # none of its own, so what reaches a host is whatever it configured.
        self._held_level = held_level

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno < (self._held_level or _inherited_level()):
            return
        # Propagation, by hand, from the logger above: every ancestor handler
        # that wants the record, and no further than one that stops the walk.
        # `logging` would also fall back to `lastResort` where it found no
        # handler at all, which real propagation from here never does — the
        # package logger itself always has one.
        logger = logging.getLogger(PACKAGE).parent
        while logger is not None:
            for handler in logger.handlers:
                if record.levelno >= handler.level:
                    handler.handle(record)
            logger = logger.parent if logger.propagate else None


class _DebugWhileWatched:
    """Holds the ``waystation`` logger at DEBUG while any run file is open.

    A run file wants every record, but a record the logger never created
    can't be handled — and without ``configure_logging`` the hierarchy sits
    at the root's WARNING. So the level goes to DEBUG, and, because that
    would push agent output into a host application's own handlers, the
    logger stops propagating and relays what the host was getting anyway.
    Our console filters on its own remembered level for the same reason
    (ADR-0026): what any other handler sees is unchanged.

    Both are given back once the last run lets go.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._watchers = 0
        self._restore: tuple[int, bool, _RelayAbove] | None = None

    def acquire(self) -> None:
        with self._lock:
            if self._watchers == 0:
                self._hold()
            self._watchers += 1

    def release(self) -> None:
        with self._lock:
            self._watchers -= 1
            if self._watchers == 0:
                self._give_back()

    def _hold(self) -> None:
        logger = logging.getLogger(PACKAGE)
        if logger.isEnabledFor(logging.DEBUG):
            return  # already as low as a run file needs; nothing to hold
        relay = _RelayAbove(logger.level)
        self._restore = (logger.level, logger.propagate, relay)
        logger.addHandler(relay)
        logger.propagate = False
        logger.setLevel(logging.DEBUG)

    def _give_back(self) -> None:
        if self._restore is None:
            return
        level, propagate, relay = self._restore
        self._restore = None
        logger = logging.getLogger(PACKAGE)
        logger.removeHandler(relay)
        logger.propagate = propagate
        # Only give back what we took: a configure_logging call during the
        # window is the script's decision and outranks ours.
        if logger.level == logging.DEBUG:
            logger.setLevel(level)


_DEBUG_WHILE_WATCHED = _DebugWhileWatched()


def _safely[**P](
    method: Callable[Concatenate[Any, P], None],
) -> Callable[Concatenate[Any, P], None]:
    """Let a built-in observer fail without failing the run (ADR-0026).

    Observability that can break the work is worse than none: a full disk or
    an unwritable directory costs a log line, never an agent's commits.
    """

    @wraps(method)
    def guarded(self: Any, *args: P.args, **kwargs: P.kwargs) -> None:
        try:
            method(self, *args, **kwargs)
        except Exception as exc:
            logger.error(
                "%s.%s failed and was ignored: %r",
                type(self).__name__,
                method.__name__,
                exc,
                exc_info=exc,
            )

    # functools.wraps hides the signature behind _Wrapped; the call is the same.
    return cast("Callable[Concatenate[Any, P], None]", guarded)


# The last line of a file whose run never fired run_end: cancelled, most
# likely, whose log line just above says where its series went.
_NO_RESULT_FOOTER = "--- no result ---"


class RunLogFiles(HookBundle):
    """One tail-able file per run under ``directory``, named for the run.

    The file holds the prompt, the agent's raw output (stderr lines marked),
    and every log record the run produced — its lifecycle lines and whatever
    a hook wrote to ``ctx.log`` — interleaved in the order they happened, each
    flushed as it lands so ``tail -f`` keeps up with a run in flight.

    Registered like any bundle: ``Flow(hooks=[RunLogFiles("logs/")])``, or per
    run with ``.hooks(...)``. A file ends with a footer saying how its run
    ended. A cancelled run has no result to say (ADR-0017), so its file ends
    ``--- no result ---`` once the task that ran it ends — at once for a
    fan-out's runs, which each run in a task of their own. Opened as a block,
    ``with RunLogFiles("logs/") as files:``, it ends every file still open
    when the block does, whatever task ran it (ADR-0026).
    """

    def __init__(self, directory: Path | str) -> None:
        self.directory = Path(directory)
        self._open: dict[str, _OpenRunFile] = {}

    @override
    @_safely
    def on_run_start(self, ctx: RunContext) -> None:
        stem = f"{ctx.name}-{ctx.run_id}" if ctx.name else ctx.run_id
        file = _FlushingFile(self.directory / f"{stem}.log")
        handler = _RunFileHandler(ctx.run_id, file)
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(message)s")
        )
        run_id = ctx.run_id
        task = _running_task()

        def on_task_done(done: asyncio.Task[Any]) -> None:
            # Still open once the task that ran it has ended, so run_end
            # never came: a cancelled run's last word is its task ending.
            self._finish(run_id, _NO_RESULT_FOOTER)

        # Recorded before the level is held, so whatever ends the file always
        # finds the entry and always gives the level back.
        self._open[run_id] = _OpenRunFile(file, handler, task, on_task_done)
        if task is not None:
            task.add_done_callback(on_task_done)
        _DEBUG_WHILE_WATCHED.acquire()
        logging.getLogger(PACKAGE).addHandler(handler)
        file.write(f"=== run {run_id} on {ctx.repo} ===")
        file.write(f"--- prompt ({len(ctx.prompt)} chars) ---")
        file.write(ctx.prompt)
        file.write("--- output ---")
        ctx.log.info("run log: %s", file.path)

    @override
    @_safely
    def on_agent_output(self, ctx: RunContext, line: AgentLine) -> None:
        open_file = self._open.get(ctx.run_id)
        if open_file is None:
            return
        prefix = "[stderr] " if line.stream == "stderr" else ""
        open_file.file.write(f"{prefix}{line.raw}")

    @override
    @_safely
    def on_run_end(
        self,
        ctx: RunContext,
        result: RunResult[Any],
    ) -> None:
        self._finish(ctx.run_id, _footer(result))

    def close(self) -> None:
        """End every file still open with ``--- no result ---``.

        A file closes by itself when its run ends, or when the task that ran
        it does. What is left is a run cancelled inside a task that went on —
        under ``asyncio.timeout``, say — or one still going. A run that starts
        afterwards opens its own file as usual.
        """
        for run_id in list(self._open):
            self._finish(run_id, _NO_RESULT_FOOTER)

    def __enter__(self) -> RunLogFiles:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # Guarded like the hooks: a full disk costs a log line, never the work or
    # the exception a block is already raising (ADR-0026).
    @_safely
    def _finish(self, run_id: str, footer: str) -> None:
        """Write ``footer``, detach the run's handler, give the level back."""
        # Popped first, so whichever of run_end, the task ending and close
        # comes first finishes the file, and the others find nothing.
        open_file = self._open.pop(run_id, None)
        if open_file is None:
            return
        file, handler, task, on_task_done = open_file
        try:
            if task is not None:
                task.remove_done_callback(on_task_done)
            file.write(footer)
        finally:
            logging.getLogger(PACKAGE).removeHandler(handler)
            _DEBUG_WHILE_WATCHED.release()
            file.close()


def _footer(result: RunResult[Any]) -> str:
    """The last line of a run file: how the agent ended, or why it didn't."""
    if isinstance(result, RunFailed) and isinstance(result.failure, TimedOut):
        bound = result.failure
        return (
            f"--- TIMEOUT: {bound.bound} after {bound.elapsed:.1f}s "
            f"(limit {bound.limit:.1f}s) ---"
        )
    if result.agent is not None:
        return f"--- exit code: {result.agent.exit_code} ---"
    failure = type(result.failure).__name__ if isinstance(result, RunFailed) else "none"
    return f"--- no agent exit: {failure} ---"


class EventLog(HookBundle):
    """One JSONL object per lifecycle event, for a whole flow.

    Each line carries ``ts``, ``event``, ``run_id`` and ``name``, then the
    event's own fields flattened alongside — an ``agent_end`` line has the
    exit code and elapsed, an ``integrated`` line has the target and what
    landed. One file for every run in the flow, so a fan-out reads back in the
    order things actually happened.

    The agent's output is left out unless ``include_output=True``: it is the
    bulk of a run, and a log meant for analysis is usually not the place for it.
    """

    def __init__(self, path: Path | str, *, include_output: bool = False) -> None:
        self.path = Path(path)
        self.include_output = include_output
        self._lock = threading.Lock()
        self._file: _FlushingFile | None = None

    @override
    @_safely
    def on_run_start(self, ctx: RunContext) -> None:
        self._write(
            "run_start", ctx, {"repo": str(ctx.repo), "prompt_chars": len(ctx.prompt)}
        )

    @override
    @_safely
    def on_workspace_ready(self, ctx: RunContext) -> None:
        self._write("workspace_ready", ctx, {"base_sha": ctx.base_sha})

    @override
    @_safely
    def on_sandbox_ready(self, ctx: RunContext) -> None:
        self._write("sandbox_ready", ctx)

    @override
    @_safely
    def on_agent_output(self, ctx: RunContext, line: AgentLine) -> None:
        if not self.include_output:
            return
        self._write("agent_output", ctx, {"stream": line.stream, "raw": line.raw})

    @override
    @_safely
    def on_agent_end(self, ctx: RunContext, exit: AgentExit) -> None:
        self._write("agent_end", ctx, _fields(exit))

    @override
    @_safely
    def on_integrated(self, ctx: RunContext, report: IntegrationReport) -> None:
        self._write("integrated", ctx, _fields(report))

    @override
    @_safely
    def on_run_end(
        self,
        ctx: RunContext,
        result: RunResult[Any],
    ) -> None:
        fields = _fields(result)
        if isinstance(result, RunFailed):
            # The union's tag, which flattening the fields would otherwise lose:
            # without it a timeout and a refusal read alike.
            fields["failure_kind"] = type(result.failure).__name__
        self._write("run_end", ctx, fields)

    def _write(
        self,
        event: HookName,
        ctx: RunContext,
        fields: Mapping[str, Any] | None = None,
    ) -> None:
        line: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(),
            "event": event,
            "run_id": ctx.run_id,
            "name": ctx.name,
        }
        for key, value in (fields or {}).items():
            # The envelope is the authority on which run this is.
            line.setdefault(key, value)
        with self._lock:
            # Opened on the first event rather than at construction, so an
            # unwritable path costs a logged ERROR inside a guarded hook
            # instead of raising while a flow script is still being built.
            if self._file is None:
                self._file = _FlushingFile(self.path)
            self._file.write(json.dumps(line, default=repr))

    def close(self) -> None:
        """Release the file. A flow has no end event, so a script that wants
        the handle back scopes the log itself: ``with EventLog(path) as log:``.
        Every line is already flushed, so nothing is lost without this.
        """
        with self._lock:
            if self._file is not None:
                self._file.close()
                self._file = None

    def __enter__(self) -> EventLog:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _fields(value: object) -> dict[str, Any]:
    """A value's fields as JSON, with ``repr`` for anything pydantic can't take.

    ``TypeAdapter`` cannot even build a schema for a result: ``Errored`` holds
    an exception and ``OutcomeInvalid`` a ``ValidationError``. This is the same
    serializer underneath, told what to do when it meets one.
    """
    dumped = to_jsonable_python(value, fallback=repr, serialize_unknown=True)
    return dumped if isinstance(dumped, dict) else {"value": dumped}


@dataclass(slots=True)
class _Row:
    """One run as the dashboard shows it; hooks write it, the display reads it."""

    label: str
    clock: Clock
    started: float
    stage: Stage = "workspace"
    said: str = ""
    cost_usd: float | None = None
    ended: float | None = None
    glyph: Text | None = None

    def elapsed(self) -> float:
        # The clock the run started under, not whatever the reading thread
        # has: the display refreshes from a thread no context reaches.
        until = self.clock.monotonic() if self.ended is None else self.ended
        return until - self.started


class Dashboard(HookBundle):
    """A live table of runs, one row each, drawn beneath the scrolling log.

    Open it around the runs it watches and register it like any bundle::

        async with Dashboard() as dashboard:
            await flow.run(prompt).hooks(dashboard)

    A row shows the run's name (its id when unnamed), the stage it is in, how
    long it has been going, the last thing the agent said, what it cost once
    the agent reports usage, and a glyph for its result. What the agent said
    is the text its provider parsed out of a line, so a raw JSON event or a
    stderr line never lands there. Rows stay once their run ends, so a
    finished batch reads back whole; a run still going when the dashboard
    closes (a cancelled run fires no ``run_end``) is marked as having no
    result.

    Without a ``console`` it draws on the one ``configure_logging`` installed,
    so log lines print above the table rather than through it.
    """

    def __init__(self, console: Console | None = None) -> None:
        self.console = console
        self._rows: dict[str, _Row] = {}
        # Hooks add rows on the event loop while the display reads them from
        # its refresh thread.
        self._lock = threading.Lock()
        self._live: Live | None = None

    async def __aenter__(self) -> Dashboard:
        self._start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        # A cancelled run fires no run_end (ADR-0017), so a row still going
        # now never will be: stop its clock rather than leave it ticking.
        with self._lock:
            rows = list(self._rows.values())
        for row in rows:
            if row.glyph is None:
                row.ended = row.clock.monotonic()
                row.glyph = _NO_RESULT
        self._stop()

    # Guarded like the hooks: a terminal that went away mid-batch costs a log
    # line, never the work or the exception the script is already raising.
    @_safely
    def _start(self) -> None:
        console = self.console or configured_console() or Console(stderr=True)
        # stdout stays the script's: redirecting it onto a stderr console
        # would move a script's printed results out of a pipe.
        self._live = Live(self, console=console, redirect_stdout=False)
        self._live.start(refresh=True)

    @_safely
    def _stop(self) -> None:
        live, self._live = self._live, None
        if live is not None:
            live.stop()

    @override
    @_safely
    def on_run_start(self, ctx: RunContext) -> None:
        clock = get_clock()
        row = _Row(label=ctx.name or ctx.run_id, clock=clock, started=clock.monotonic())
        with self._lock:
            self._rows[ctx.run_id] = row

    @override
    @_safely
    def on_workspace_ready(self, ctx: RunContext) -> None:
        self._move(ctx, "sandbox")

    @override
    @_safely
    def on_sandbox_ready(self, ctx: RunContext) -> None:
        self._move(ctx, "agent")

    @override
    @_safely
    def on_agent_output(self, ctx: RunContext, line: AgentLine) -> None:
        row = self._rows.get(ctx.run_id)
        said = _said(line)
        if row is not None and said is not None:
            row.said = said

    @override
    @_safely
    def on_agent_end(self, ctx: RunContext, exit: AgentExit) -> None:
        # Collect follows the agent, and integrate starts with no hook of its
        # own, so this is as far as the dashboard can see until it lands.
        self._move(ctx, "collect")
        row = self._rows.get(ctx.run_id)
        if row is not None and exit.usage is not None:
            row.cost_usd = exit.usage.cost_usd

    @override
    @_safely
    def on_integrated(self, ctx: RunContext, report: IntegrationReport) -> None:
        self._move(ctx, "integrate")

    @override
    @_safely
    def on_run_end(self, ctx: RunContext, result: RunResult[Any]) -> None:
        row = self._rows.get(ctx.run_id)
        if row is None:
            return
        if isinstance(result, RunFailed):
            row.stage = result.stage
        elif isinstance(result, RunConflicted):
            row.stage = "integrate"
        row.ended = row.clock.monotonic()
        row.glyph = _GLYPHS[type(result)]

    def _move(self, ctx: RunContext, stage: Stage) -> None:
        row = self._rows.get(ctx.run_id)
        if row is not None:
            row.stage = stage

    def __rich__(self) -> Table:
        """The table as it stands, so ``console.print(dashboard)`` works too."""
        table = Table(box=box.SIMPLE_HEAD)
        table.add_column("run", no_wrap=True)
        table.add_column("stage", no_wrap=True)
        table.add_column("elapsed", justify="right", no_wrap=True)
        table.add_column("last line", no_wrap=True, overflow="ellipsis")
        table.add_column("cost", justify="right", no_wrap=True)
        table.add_column("", no_wrap=True)
        with self._lock:
            rows = list(self._rows.values())
        for row in rows:
            table.add_row(
                Text(row.label),
                row.stage,
                _clock_face(row.elapsed()),
                # Text, never markup: an agent's "[/]" is something it said.
                Text(row.said),
                "" if row.cost_usd is None else f"${row.cost_usd:.2f}",
                row.glyph or "",
            )
        return table


_GLYPHS: Mapping[type, Text] = {
    RunSucceeded: Text("✓", style="green"),
    RunConflicted: Text("!", style="bold yellow"),
    RunFailed: Text("✗", style="bold red"),
}
_NO_RESULT = Text("–", style="dim")


def _said(line: AgentLine) -> str | None:
    """The last thing a line said, if its provider found any text in it.

    Only what the provider parsed counts: a raw line may be a JSON event or a
    stderr diagnostic, and neither is something the agent said.
    """
    said = None
    for event in line.events:
        if isinstance(event, AgentText):
            spoken = [part.strip() for part in event.text.splitlines() if part.strip()]
            if spoken:
                said = spoken[-1]
    return said


def _clock_face(seconds: float) -> str:
    """Elapsed time the way a person reads it: ``4.2s``, ``1m05s``, ``2h07m``."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, secs = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m{secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"
