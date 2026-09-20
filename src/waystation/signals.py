"""Opt-in signal handling: a terminating signal cancels a flow like Ctrl-C does.

The library installs no handler of its own — a process-global handler fights
whatever host it runs in, a web service or a test runner, exactly the way an
auto-installed logging handler would. A flow script opts in by calling
``handle_signals()`` beside ``configure_logging()`` (ADR-0017).
"""

from __future__ import annotations

import asyncio
import signal
import sys
import threading
from types import FrameType

__all__ = ["handle_signals"]

# What a shell, a CI runner or `docker stop` ends a process with. Windows has
# no SIGTERM to catch (os.kill there is TerminateProcess), but a console can
# send a process group Ctrl-Break.
if sys.platform == "win32":
    _SHUTDOWN = (signal.SIGINT, signal.SIGBREAK)
else:
    _SHUTDOWN = (signal.SIGINT, signal.SIGTERM)


def handle_signals() -> None:
    """Cancel the calling task once on SIGINT or SIGTERM (SIGBREAK on Windows).

    Call it inside the main task, beside ``configure_logging()``. The first
    signal cancels that task, so the run it is awaiting keeps its series and
    tears its sandbox down just as Ctrl-C does under ``asyncio.run``.

    Each handled signal then goes back to ``SIG_DFL``, so a second one gets the
    operating system's default and ends the process, however wedged its
    cleanup is. A signal arriving once the task is done gets that default too:
    there is nothing left to cancel. Calling this again re-arms it for the
    calling task; the handlers it replaced are never restored.

    Raises:
        RuntimeError: When called outside a running task, or off the main
            thread, where Python installs no signal handler at all.
    """
    if threading.current_thread() is not threading.main_thread():
        # `signal.signal` says only "signal only works in main thread of the
        # main interpreter", which is true and no help. Nothing is armed, so
        # a signal still falls through to the default (ADR-0017).
        raise RuntimeError(
            "handle_signals() only works on the main thread: Python installs "
            "signal handlers there and nowhere else. A flow running on another "
            "thread is cancelled by whatever runs it, not by a signal."
        )
    task = asyncio.current_task()
    if task is None:
        raise RuntimeError("handle_signals() must be called inside a task")
    loop = task.get_loop()

    def _cancel(signum: int) -> None:
        if not task.cancel(f"received {signal.Signals(signum).name}"):
            signal.raise_signal(signum)  # the task ended first: nothing to cancel

    def _on_signal(signum: int, frame: FrameType | None) -> None:
        for sig in _SHUTDOWN:
            signal.signal(sig, signal.SIG_DFL)
        if task.done() or loop.is_closed():
            signal.raise_signal(signum)
            return
        # A handler runs between bytecodes, maybe inside the loop's own code;
        # the threadsafe call is the one that also wakes a loop parked in select.
        loop.call_soon_threadsafe(_cancel, signum)

    for sig in _SHUTDOWN:
        signal.signal(sig, _on_signal)
