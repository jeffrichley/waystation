"""The waystation logger hierarchy, its opt-in console, and redaction.

Importing waystation installs no handler but a ``NullHandler``: the library is
silent inside a host application until a flow script calls
``configure_logging`` (ADR-0001). Levels are stdlib all the way down, so a
script that wants only the agent's chatter can say::

    configure_logging("INFO")
    logging.getLogger("waystation.agent.output").setLevel(logging.DEBUG)

The records themselves are an interface, not just output: ``waystation.run``
carries one per lifecycle event, including the two no hook delivers, and
``run_logger`` puts an adapter's own lines on the same channel. The names,
levels and extras are documented in ``docs/log-records.md``.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, MutableMapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Literal

from rich.console import Console
from rich.logging import RichHandler

# redact_argv lives in its own leaf module because ``results`` needs it and
# must not import this one; observability is its public home.
from waystation._redaction import redact_argv

__all__ = ["RunEvent", "configure_logging", "log_argv", "redact_argv", "run_logger"]

RunEvent = Literal[
    "run_start",
    "workspace_ready",
    "sandbox_ready",
    # The two with no hook behind them: the agent stage opens between
    # sandbox_ready and the first output line, and a cancelled run has no
    # result for run_end (ADR-0017). The channel is how an observer sees them.
    "agent_start",
    "agent_end",
    "integrated",
    "run_end",
    "cancelled",
]
"""What a record on the run channel says happened; ``docs/log-records.md``."""

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


def _tagged(name: str) -> logging.Logger:
    """A logger, by full name, that stamps the running run onto its records.

    Not a plain lookup: it installs the filter that does the stamping, which
    ``logging`` applies only to the logger the call was made on.
    """
    logger = logging.getLogger(name)
    logger.addFilter(_RUN_TAG)
    return logger


def _stream(name: str) -> logging.Logger:
    """One stream on the channel, under the package logger and unguarded.

    What ``run_logger`` is minus its guard, because the guard exists to keep
    a caller off the names below and waystation owning its own is not that.
    """
    return _tagged(f"{PACKAGE}.{name}")


def package_logger() -> logging.Logger:
    """The package logger itself: where waystation's own troubles go.

    A failure that arrived after the run already failed (ADR-0024), or a
    built-in observer that failed and was ignored (ADR-0026) — at ERROR, and
    tagged, so it reaches the file of the run it happened in.
    """
    return _tagged(PACKAGE)


def run_logger(name: str) -> logging.Logger:
    """A logger on the run channel: its lines carry the run and reach its observers.

    For an adapter of your own — a ``SandboxBackend``, an ``AgentProvider`` —
    whose lines should land in a run's ``RunLogFiles`` file beside the shipped
    ones::

        _log = run_logger("myco.docker")   # -> waystation.myco.docker
        _log.info("reusing the warm container")

    ``name`` lands **under** ``waystation``, and that is what puts it on the
    channel: a run's observers attach to the package logger, and ``logging``
    routes by dotted name alone, so a logger outside the hierarchy is never
    reached however it is tagged. Celery's ``get_task_logger`` and Prefect's
    ``get_run_logger`` parent a caller's logger the same way, for the same
    reason. Your own loggers are untouched by this — keep ``myco.docker`` for
    lines that are your library's business rather than a run's.

    The records it makes carry ``run_id`` and ``run_name``, so an observer can
    tell one run of a fan-out from another. They carry no ``event``: that is
    the lifecycle channel's, and yours are lines, not events
    (``docs/log-records.md``).

    Raises:
        ValueError: When ``name`` is one waystation already ships, or is
            already spelled under the package. Writing to ``run`` would put
            records with no ``event`` on the lifecycle channel, where every
            record is documented to carry one, and every observer reading it
            would have to cope with a shape the docs rule out.
    """
    if not name:
        msg = "run_logger needs a name for your stream, e.g. run_logger('myco.docker')"
        raise ValueError(msg)
    if name in SHIPPED_STREAMS:
        msg = (
            f"{name!r} is a stream waystation ships, and its records are "
            f"documented down to their extras: pick a name of your own, such "
            f"as run_logger('myco.{name}')"
        )
        raise ValueError(msg)
    if name == PACKAGE or name.startswith(f"{PACKAGE}."):
        msg = (
            f"run_logger puts {name!r} under {PACKAGE!r} for you, so this one "
            f"would land at 'waystation.{name}': pass the part after it"
        )
        raise ValueError(msg)
    return _stream(name)


logging.getLogger(PACKAGE).addHandler(logging.NullHandler())

# The streams waystation ships. A user's adapter joins the channel exactly as
# these do — `run_logger` is `_stream` plus a guard keeping callers off these
# names — so there is no privileged path in, only names already spoken for.

SHIPPED_STREAMS = frozenset({"run", "agent", "agent.output", "hook", "git", "sandbox"})

RUN = _stream("run")
"""One INFO line per lifecycle event of a run, each tagged with its ``event``."""

AGENT = _stream("agent")
"""An agent provider's own lines — which credential its preflight found, at INFO."""

AGENT_OUTPUT = _stream("agent.output")
"""Every line the agent emits, at DEBUG: an N-way fan-out is unreadable at INFO."""

HOOK = _stream("hook")
"""Where ``ctx.log`` writes, so a hook author's lines are separable from ours."""

GIT = _stream("git")
"""Host git command lines, at DEBUG."""

SANDBOX = _stream("sandbox")
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


def would_reach(record: logging.LogRecord, level: int) -> bool:
    """Whether something holding ``level`` would have seen ``record`` anyway.

    A run-file observer turns the hierarchy down to DEBUG so the records it
    wants exist to be written (ADR-0026). Everything else — our console, and
    the handlers a host application owns — answers to the level it chose, so
    the extra records reach neither. A logger the script tuned itself still
    gets through, which is the whole point of per-logger tuning: one rule,
    used everywhere insulation is needed, so the two cannot drift apart.
    """
    if record.levelno >= level:
        return True
    return logging.getLogger(record.name).level != logging.NOTSET


class _ConsoleLevel(logging.Filter):
    """Holds the console to the level ``configure_logging`` was given."""

    def __init__(self, level: int) -> None:
        super().__init__()
        self.level = level

    def filter(self, record: logging.LogRecord) -> bool:
        return would_reach(record, self.level)


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
