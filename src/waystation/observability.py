"""The waystation logger hierarchy, its opt-in console, and redaction.

Importing waystation installs no handler but a ``NullHandler``: the library is
silent inside a host application until a flow script calls
``configure_logging`` (ADR-0001). Levels are stdlib all the way down, so a
script that wants only the agent's chatter can say::

    configure_logging("INFO")
    logging.getLogger("waystation.agent.output").setLevel(logging.DEBUG)
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, MutableMapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.logging import RichHandler

# redact_argv lives in its own leaf module because ``results`` needs it and
# must not import this one; observability is its public home.
from waystation._redaction import redact_argv
from waystation.results import (
    AgentExit,
    IntegrationReport,
    RunFailed,
    RunSucceeded,
)

__all__ = ["configure_logging", "log_argv", "redact_argv", "tagged_logger"]

PACKAGE = "waystation"

# How much of a prompt's first line an INFO line may carry. The rest of the
# prompt never reaches a log record at all.
PROMPT_HEAD = 80

# The run whose stages are executing on this task, for records logged too deep
# to be handed one — git and sandbox argv. asyncio copies the context into
# every task and thread, so a fan-out's runs never see each other's.
_current_run: ContextVar[tuple[str, str | None] | None] = ContextVar(
    "waystation_run", default=None
)


class _RunTag(logging.Filter):
    """Stamp the bound run onto records that were not given one."""

    def filter(self, record: logging.LogRecord) -> bool:
        bound = _current_run.get()
        if bound is not None:
            run_id, name = bound
            if not hasattr(record, "run_id"):
                record.run_id = run_id
            if not hasattr(record, "run_name"):
                record.run_name = name
        return True


_RUN_TAG = _RunTag()


def tagged_logger(name: str) -> logging.Logger:
    """A ``waystation`` logger that stamps the running run onto its records.

    Not a plain lookup: it installs the filter that does the stamping, which
    ``logging`` applies only to the logger the call was made on.
    """
    logger = logging.getLogger(name)
    logger.addFilter(_RUN_TAG)
    return logger


logging.getLogger(PACKAGE).addHandler(logging.NullHandler())

RUN = tagged_logger(f"{PACKAGE}.run")
"""One INFO line per lifecycle event of a run."""

AGENT_OUTPUT = tagged_logger(f"{PACKAGE}.agent.output")
"""Every line the agent emits, at DEBUG: an N-way fan-out is unreadable at INFO."""

HOOK = tagged_logger(f"{PACKAGE}.hook")
"""Where ``ctx.log`` writes, so a hook author's lines are separable from ours."""

GIT = tagged_logger(f"{PACKAGE}.git")
"""Host git command lines, at DEBUG."""

SANDBOX = tagged_logger(f"{PACKAGE}.sandbox")
"""Sandbox lifecycle and the command lines it execs, at DEBUG."""


def log_argv(logger: logging.Logger, argv: Sequence[str]) -> None:
    """Log one command line at DEBUG, with its credentials elided."""
    logger.debug("%s", " ".join(redact_argv(argv)))


class RunLoggerAdapter(logging.LoggerAdapter[logging.Logger]):
    """Tags records with the run, keeping whatever extras the caller passed.

    The run's name rides as ``run_name``: ``name`` is the logger's own record
    attribute, and stdlib refuses to let an ``extra`` overwrite it.
    """

    def process(
        self, msg: Any, kwargs: MutableMapping[str, Any]
    ) -> tuple[Any, MutableMapping[str, Any]]:
        extra = dict(self.extra or {})
        extra.update(kwargs.get("extra") or {})
        kwargs["extra"] = extra
        return msg, kwargs


def _head(prompt: str) -> str:
    """The prompt's first line, bounded; never more of it than that."""
    first = prompt.splitlines()[0] if prompt else ""
    return first if len(first) <= PROMPT_HEAD else f"{first[: PROMPT_HEAD - 1]}…"


class RunLog:
    """One run's lifecycle lines, written to the loggers every run shares.

    ``hooks`` is what ``ctx.log`` hands a hook author: their lines, already
    tagged with the run. The adapters tag explicitly rather than leaning on
    ``bound()``, so a line still carries its run when it is logged from
    somewhere the run's context never reached — a hook that kept ``ctx.log``
    and writes from a task of its own.
    """

    __slots__ = ("_bound", "_output", "_run", "hooks")

    def __init__(self, run_id: str, name: str | None) -> None:
        extra = {"run_id": run_id, "run_name": name}
        self._bound = (run_id, name)
        self._run = RunLoggerAdapter(RUN, extra)
        self._output = RunLoggerAdapter(AGENT_OUTPUT, extra)
        self.hooks = RunLoggerAdapter(HOOK, extra)

    @contextmanager
    def bound(self) -> Iterator[None]:
        """Tag records logged too deep to be handed the run — git, sandbox argv."""
        token = _current_run.set(self._bound)
        try:
            yield
        finally:
            _current_run.reset(token)

    def run_start(self, repo: Path) -> None:
        self._run.info("run start: %s", repo)

    def workspace_ready(self, base_sha: str) -> None:
        self._run.info("workspace ready: base %s", base_sha[:12])

    def sandbox_ready(self) -> None:
        self._run.info("sandbox up")

    def agent_start(self, prompt: str) -> None:
        self._run.info(
            "agent start: prompt %d chars, first line %r", len(prompt), _head(prompt)
        )

    def agent_end(self, exit: AgentExit) -> None:
        self._run.info("agent end: exit %d in %.1fs", exit.exit_code, exit.elapsed)

    def agent_output(self, stream: str, raw: str) -> None:
        self._output.debug("%s%s", "[stderr] " if stream == "stderr" else "", raw)

    def integrated(self, report: IntegrationReport) -> None:
        landed = len(report.landed)
        self._run.info(
            "integrated: %d commit%s onto %s",
            landed,
            "" if landed == 1 else "s",
            report.target,
        )

    def run_end(self, result: RunSucceeded[Any] | RunFailed) -> None:
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


class _RunTagFormatter(logging.Formatter):
    """Prefix each line with the run's name, falling back to its id."""

    def format(self, record: logging.LogRecord) -> str:
        message = super().format(record)
        tag = getattr(record, "run_name", None) or getattr(record, "run_id", None)
        return f"[{tag}] {message}" if tag else message


class _WaystationHandler(RichHandler):
    """The one handler ``configure_logging`` owns, and the only one it replaces."""


def configure_logging(
    level: int | str = "INFO", *, console: Console | None = None
) -> Console:
    """Install the one ``RichHandler`` waystation logs through, and return its console.

    Writes to **stderr**: stdout is the agent's Outcome channel. Calling this
    again replaces the handler it installed before rather than stacking a
    second one, and returns the same console unless a new one is passed. A
    handler the host application attached itself is left alone. The level lands
    on the ``waystation`` logger, so per-logger levels still win.

    Records keep propagating to the root logger, as a library's should: a host
    that collects waystation's logs still gets them, and where that host's own
    handler writes is the host's business.
    """
    logger = logging.getLogger(PACKAGE)
    ours = [h for h in logger.handlers if isinstance(h, _WaystationHandler)]
    if console is None:
        console = ours[0].console if ours else Console(stderr=True)
    for handler in ours:
        logger.removeHandler(handler)
    installed = _WaystationHandler(
        console=console, show_path=False, rich_tracebacks=True
    )
    installed.setFormatter(_RunTagFormatter("%(message)s"))
    logger.addHandler(installed)
    logger.setLevel(level)
    return console
