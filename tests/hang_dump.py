"""Name the await a hung test is stuck on, before anything kills the worker.

`faulthandler` dumps OS threads, and a suspended coroutine sits on none of
them: a wedged async test shows the event loop idle in `_poll` and nothing
about which `await` never came back. That is exactly the evidence #105 needed
and did not have, twice. This fills the gap — every unfinished task, and the
whole chain of awaits inside it — and it runs *before* `faulthandler_timeout`,
because after that the countdown to `os._exit` is already short.

It writes a file. Under xdist a worker's output is not reliably anyone's to
read, and a worker ended with `os._exit` flushes nothing it had captured, so
a file on disk is the only channel that survives the thing it is describing;
CI uploads the directory. A copy also goes to the descriptor pytest keeps for
its own faulthandler, which is the one stream that does reach the log.
"""

from __future__ import annotations

import asyncio
import gc
import linecache
import os
import re
import sys
import threading
import traceback
from collections.abc import Iterator
from pathlib import Path
from types import FrameType
from typing import Any

__all__ = ["DUMPS", "arm"]

DUMPS = Path(__file__).resolve().parent.parent / "hang-dumps"
"""Where a dump lands; CI and the soak workflow upload this directory."""

# How long before `faulthandler_timeout` this fires. Small, because it only
# has to land first — the two are describing the same moment from different
# sides, and a dump from well before the hang is a dump of something else.
_HEAD_START = 5.0


def arm(nodeid: str, after: float) -> threading.Timer:
    """Start a timer that dumps every unfinished task if ``nodeid`` is still going.

    Returns the timer, for the caller to cancel when the test ends. ``after``
    is the test's ``faulthandler_timeout``, so the two can never drift apart.
    """
    timer = threading.Timer(max(1.0, after - _HEAD_START), _dump, args=(nodeid,))
    timer.daemon = True
    timer.start()
    return timer


def _unfinished() -> list[asyncio.Task[Any]]:
    """Every task still running, found without a loop to ask for them.

    ``asyncio.all_tasks`` wants the loop, and this runs on a thread that never
    had one — pytest-asyncio makes the loop inside the test call, where a hook
    cannot reach it. The object graph holds them all either way, and by the
    time this runs the test has been wedged for the better part of a minute,
    so what a sweep costs is not a consideration.
    """
    return [
        obj
        for obj in gc.get_objects()
        if isinstance(obj, asyncio.Task) and not obj.done()
    ]


def _frames(task: asyncio.Task[Any]) -> Iterator[FrameType | None]:
    """Every frame inside one task, outermost first.

    Not ``Task.get_stack()``: for a *suspended* task that returns exactly one
    frame — the coroutine's own, whose ``f_back`` is ``None`` while it is off
    the stack (CPython's ``base_tasks._task_get_stack``, and the docs say so
    outright). The nested awaits hang off ``cr_await`` instead. Walking it by
    hand is the difference between naming the await that wedged and naming
    the function the task was created with, which is already known.
    """
    coro: Any = task.get_coro()
    seen: set[int] = set()
    while coro is not None and id(coro) not in seen:
        seen.add(id(coro))
        yield (
            getattr(coro, "cr_frame", None)
            or getattr(coro, "gi_frame", None)
            or getattr(coro, "ag_frame", None)
        )
        # The chain ends at a C-level awaitable — a Future's iterator — which
        # is where one task awaits another; `_fut_waiter` names that edge.
        coro = next(
            (
                getattr(coro, attr)
                for attr in ("cr_await", "ag_await", "gi_yieldfrom")
                if hasattr(coro, attr)
            ),
            None,
        )


def _where(frame: FrameType | None) -> str:
    if frame is None:
        return "      <no python frame: a C-level awaitable>"
    code = frame.f_code
    source = linecache.getline(code.co_filename, frame.f_lineno).strip()
    return (
        f'      File "{code.co_filename}", line {frame.f_lineno}, in {code.co_name}\n'
        f"        {source}"
    )


def _report(nodeid: str, worker: str) -> str:
    lines = [f"still running after the hang timer: {nodeid} [{worker}]", ""]
    try:
        tasks = _unfinished()
    except Exception:  # a diagnostic must not become the failure it explains
        return "\n".join([*lines, "could not sweep for tasks:", traceback.format_exc()])

    if not tasks:
        lines.append("no unfinished asyncio tasks: the wedge is not an await")
    for task in tasks:
        lines.append(f"--- {task.get_name()}: {task.get_coro()!r}")
        lines.append(f"    blocked on: {getattr(task, '_fut_waiter', None)!r}")
        try:
            # Outermost first, so the last line is the await that never
            # came back — read it the way you read a traceback.
            lines.extend(_where(frame) for frame in _frames(task))
        except Exception:
            lines.append("      (chain unavailable)")
        lines.append("")
    return "\n".join(lines)


def _dump(nodeid: str) -> None:
    """Write every unfinished task and the await chain it is suspended in."""
    worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
    text = _report(nodeid, worker) + "\n"

    try:
        DUMPS.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9._-]+", "-", nodeid)[-120:]
        (DUMPS / f"{worker}-{safe}.txt").write_text(text, encoding="utf-8")
    except OSError:
        pass  # the stream below may still carry it

    _to_the_log(text)


def _to_the_log(text: str) -> None:
    """Best effort at the terminal, for a reader who has no artifact to fetch.

    ``sys.stderr`` here is pytest's capture object, and a worker ended with
    ``os._exit`` discards what it buffered — so the descriptor pytest dup'd
    for its own faulthandler is tried first: that one is execnet's surviving
    copy of the worker's real stderr, and it is why `Timeout (0:00:45)!`
    reaches the log when nothing else does.
    """
    config = _CONFIG[0]
    if config is not None:
        try:
            from _pytest.faulthandler import fault_handler_stderr_fd_key

            os.write(config.stash[fault_handler_stderr_fd_key], text.encode("utf-8"))
            return
        except Exception:
            pass  # a private stash may move; the plain stream is the fallback
    try:
        sys.stderr.write(text)
        sys.stderr.flush()
    except Exception:
        pass


_CONFIG: list[Any] = [None]
"""The active pytest config, set by ``conftest``; the stash lives on it."""


def remember(config: Any) -> None:
    """Hold the config, so a dump can reach pytest's own stderr descriptor."""
    _CONFIG[0] = config
