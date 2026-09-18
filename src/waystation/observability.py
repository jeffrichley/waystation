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
from typing import Any

from rich.console import Console
from rich.logging import RichHandler

# redact_argv lives in its own leaf module because ``results`` needs it and
# must not import this one; observability is its public home.
from waystation._redaction import redact_argv

__all__ = ["configure_logging", "log_argv", "redact_argv", "tagged_logger"]

# One run reaches a record three ways, and each does a different job: ``tag``
# binds it explicitly, so the record carries the run even when logged from a
# task the run never entered; ``bind_run`` fills it in for code too deep to be
# handed a run; and ``_RunTagFormatter`` renders whichever arrived.

PACKAGE = "waystation"

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


def tag(logger: logging.Logger, run_id: str, name: str | None) -> RunLoggerAdapter:
    """Bind ``logger`` to one run, so its records carry the run wherever logged."""
    return RunLoggerAdapter(logger, {"run_id": run_id, "run_name": name})


@contextmanager
def bind_run(run_id: str, name: str | None) -> Iterator[None]:
    """Tag records logged too deep to be handed the run — git and sandbox argv."""
    token = _current_run.set((run_id, name))
    try:
        yield
    finally:
        _current_run.reset(token)


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


class _RunTagFormatter(logging.Formatter):
    """Prefix each line with the run's name, falling back to its id."""

    def format(self, record: logging.LogRecord) -> str:
        message = super().format(record)
        tag = getattr(record, "run_name", None) or getattr(record, "run_id", None)
        return f"[{tag}] {message}" if tag else message


class _WaystationHandler(RichHandler):
    """The one handler ``configure_logging`` owns, and the only one it replaces."""


class _ConsoleLevel(logging.Filter):
    """Holds the console to the level ``configure_logging`` was given.

    A run-file observer turns the hierarchy down to DEBUG so the records it
    wants exist to be written (ADR-0026). That must not start printing git
    argv and every agent line to someone's terminal, so the console filters on
    its own remembered level — while a logger the script tuned itself still
    gets through, which is the whole point of per-logger tuning.
    """

    def __init__(self, level: int) -> None:
        super().__init__()
        self.level = level

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= self.level:
            return True
        return logging.getLogger(record.name).level != logging.NOTSET


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
    if console is None:
        console = configured_console() or Console(stderr=True)
    for handler in [h for h in logger.handlers if isinstance(h, _WaystationHandler)]:
        logger.removeHandler(handler)
    installed = _WaystationHandler(
        console=console, show_path=False, rich_tracebacks=True
    )
    installed.setFormatter(_RunTagFormatter("%(message)s"))
    numeric = level if isinstance(level, int) else logging.getLevelNamesMapping()[level]
    installed.addFilter(_ConsoleLevel(numeric))
    logger.addHandler(installed)
    logger.setLevel(numeric)
    return console


def configured_console() -> Console | None:
    """The console ``configure_logging`` installed, or ``None`` before it runs.

    A live display has to draw on this one: log lines printed through any
    other console would land in the middle of the display.
    """
    for handler in logging.getLogger(PACKAGE).handlers:
        if isinstance(handler, _WaystationHandler):
            return handler.console
    return None
