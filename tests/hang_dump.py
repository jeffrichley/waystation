"""Name the await a hung test is stuck on, before anything kills the worker.

`faulthandler` dumps OS threads, and a suspended coroutine sits on none of
them: a wedged async test shows the event loop idle in `_poll` and nothing
about which `await` never came back. That is exactly the evidence #105 needed
and did not have, twice. This fills the gap — every unfinished task, and the
line each one is suspended at — and it runs *before* `faulthandler_timeout`,
because after that the countdown to `os._exit` is already short.

It writes a file. Under xdist a worker's output is not reliably anyone's to
read, and a worker ended with `os._exit` flushes nothing it had captured, so
a file on disk is the only channel that survives the thing it is describing.
"""

from __future__ import annotations

import asyncio
import gc
import os
import re
import sys
import threading
import traceback
from pathlib import Path
from typing import Any

__all__ = ["DUMPS", "arm"]

DUMPS = Path(__file__).resolve().parent.parent / "hang-dumps"
"""Where a dump lands; the soak workflow uploads this directory."""

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


def _dump(nodeid: str) -> None:
    """Write every unfinished task and the line it is suspended at."""
    worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
    DUMPS.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", nodeid)[-120:]
    path = DUMPS / f"{worker}-{safe}.txt"

    lines = [f"still running after the hang timer: {nodeid} [{worker}]", ""]
    try:
        tasks = _unfinished()
    except Exception:  # a diagnostic must not become the failure it explains
        lines.append("could not sweep for tasks:\n" + traceback.format_exc())
        tasks = []

    if not tasks:
        lines.append("no unfinished asyncio tasks: the wedge is not an await")
    for task in tasks:
        lines.append(f"--- {task.get_name()}: {task.get_coro()!r}")
        try:
            stack = task.get_stack()
        except Exception:
            lines.append("    (stack unavailable)")
            continue
        if not stack:
            lines.append("    (no frames: not started, or already unwinding)")
        # Innermost last, as a traceback reads: the final line is the await.
        for frame in stack:
            where = f'{frame.f_code.co_filename}", line {frame.f_lineno}'
            lines.append(f'    File "{where} in {frame.f_code.co_name}')
        lines.append("")

    text = "\n".join(lines) + "\n"
    path.write_text(text, encoding="utf-8")
    # Best effort as well: readable straight from the log when it does survive.
    with _suppressed():
        sys.stderr.write(f"\n=== hang dump: {path} ===\n{text}")
        sys.stderr.flush()


class _suppressed:
    """Swallow whatever writing to a doomed worker's stderr raises."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: object) -> bool:
        return True
