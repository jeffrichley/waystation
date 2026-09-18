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

import json
import logging
import threading
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from typing import Any, Concatenate, NamedTuple, cast, override

from pydantic_core import to_jsonable_python

from waystation.agents.protocol import AgentLine
from waystation.hooks import HookBundle, HookName, RunContext
from waystation.observability import AGENT_OUTPUT, PACKAGE, RUN, tag, tagged_logger
from waystation.results import (
    AgentExit,
    IntegrationReport,
    RunFailed,
    RunSucceeded,
    TimedOut,
)

__all__ = ["EventLog", "RunLog", "RunLogFiles"]

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
        self, ctx: RunContext, result: RunSucceeded[Any] | RunFailed
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
        self._run.info("run end: succeeded in %.1fs", elapsed)

    # The agent's start is the one event with no hook behind it: the agent
    # stage opens between sandbox_ready and the first output line.
    def agent_start(self, prompt: str) -> None:
        """Announce the prompt by shape only — its body is never logged."""
        self._run.info(
            "agent start: prompt %d chars, first line %r", len(prompt), _head(prompt)
        )


class _FlushingFile:
    """An append-only text file whose every line is flushed as it lands.

    Both observers want the same thing: a reader — ``tail -f``, or an analysis
    script watching a fan-out — should see a line the moment it happens, not
    when the run ends. Writes come from whichever thread logged, since
    ``prepare_workspace`` runs in one while agent lines arrive on the event
    loop, so they go through a lock and no line lands inside another.
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
    """A run's file and the handler feeding it; they open and close together."""

    file: _FlushingFile
    handler: _RunFileHandler


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


class _DebugWhileWatched:
    """Holds the ``waystation`` logger at DEBUG while any run file is open.

    A run file wants every record, but a record the logger never created can't
    be handled — and without ``configure_logging`` the hierarchy sits at the
    root's WARNING. The level is restored once the last run lets go, and the
    console filters on its own remembered level so this never changes what a
    terminal shows (ADR-0026).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._watchers = 0
        self._restore: int | None = None

    def acquire(self) -> None:
        with self._lock:
            if self._watchers == 0:
                root = logging.getLogger(PACKAGE)
                self._restore = root.level
                if not root.isEnabledFor(logging.DEBUG):
                    root.setLevel(logging.DEBUG)
            self._watchers += 1

    def release(self) -> None:
        with self._lock:
            self._watchers -= 1
            if self._watchers == 0 and self._restore is not None:
                root = logging.getLogger(PACKAGE)
                # Only give back what we took: a configure_logging call during
                # the window is the script's decision and outranks ours.
                if root.level == logging.DEBUG:
                    root.setLevel(self._restore)
                self._restore = None


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


class RunLogFiles(HookBundle):
    """One tail-able file per run under ``directory``, named for the run.

    The file holds the prompt, the agent's raw output (stderr lines marked),
    and every log record the run produced — its lifecycle lines and whatever
    a hook wrote to ``ctx.log`` — interleaved in the order they happened, each
    flushed as it lands so ``tail -f`` keeps up with a run in flight.

    Registered like any bundle: ``Flow(hooks=[RunLogFiles("logs/")])``, or per
    run with ``.hooks(...)``.
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
        # Recorded before the level is held, so on_run_end always finds the
        # entry and always gives the level back.
        self._open[ctx.run_id] = _OpenRunFile(file, handler)
        _DEBUG_WHILE_WATCHED.acquire()
        logging.getLogger(PACKAGE).addHandler(handler)
        file.write(f"=== run {ctx.run_id} on {ctx.repo} ===")
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
        self, ctx: RunContext, result: RunSucceeded[Any] | RunFailed
    ) -> None:
        open_file = self._open.pop(ctx.run_id, None)
        if open_file is None:
            return
        file, handler = open_file
        try:
            file.write(_footer(result))
        finally:
            logging.getLogger(PACKAGE).removeHandler(handler)
            _DEBUG_WHILE_WATCHED.release()
            file.close()


def _footer(result: RunSucceeded[Any] | RunFailed) -> str:
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
        self, ctx: RunContext, result: RunSucceeded[Any] | RunFailed
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
