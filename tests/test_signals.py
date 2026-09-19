"""``handle_signals``: opt-in, cancels the main task once, then steps aside.

The library installs no handler by itself (ADR-0017). These tests install
handlers in the test process, so a fixture puts the previous ones back —
``handle_signals`` never does, by design.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from types import FrameType
from typing import Any

import pytest

from helpers import WORKS_UNTIL_STOPPED, a_run, subjects, workspaces
from waystation import handle_signals

# What a shell, a CI runner or `docker stop` ends a process with, per platform.
# An `if` statement, not an expression: only that is how mypy narrows platforms.
if sys.platform == "win32":
    SHUTDOWN = (signal.SIGINT, signal.SIGBREAK)
else:
    SHUTDOWN = (signal.SIGINT, signal.SIGTERM)


@pytest.fixture(autouse=True)
def handlers_put_back() -> Iterator[None]:
    before = {sig: signal.getsignal(sig) for sig in SHUTDOWN}
    try:
        yield
    finally:
        for sig, handler in before.items():
            signal.signal(sig, handler)


def _sentinel(signum: int, frame: FrameType | None) -> None:
    raise AssertionError("a replaced handler must never run again")


async def _main_waiting(started: asyncio.Event, calls: int = 1) -> None:
    for _ in range(calls):
        handle_signals()
    started.set()
    await asyncio.sleep(60)


def _raise(sig: signal.Signals) -> None:
    """Deliver ``sig`` to this process — only once someone else handles it.

    With nothing installed, SIGINT would interrupt pytest and SIGTERM would
    end the worker, so a missing handler fails the test instead.
    """
    handler: Callable[..., Any] | int | None = signal.getsignal(sig)
    assert callable(handler), f"{sig.name} is not handled"
    assert handler is not _sentinel, f"{sig.name} still has the old handler"
    assert handler is not signal.default_int_handler, f"{sig.name} is not handled"
    signal.raise_signal(sig)


@pytest.mark.unit
@pytest.mark.parametrize("sig", SHUTDOWN, ids=lambda sig: sig.name)
async def test_a_signal_cancels_the_task_that_asked_and_restores_nothing(
    sig: signal.Signals,
) -> None:
    for handled in SHUTDOWN:
        signal.signal(handled, _sentinel)
    started = asyncio.Event()
    task = asyncio.create_task(_main_waiting(started))
    await started.wait()

    _raise(sig)
    with pytest.raises(asyncio.CancelledError):
        await task

    assert task.cancelled()
    # A second signal gets the default, not whatever was installed before.
    assert [signal.getsignal(s) for s in SHUTDOWN] == [signal.SIG_DFL] * 2


@pytest.mark.unit
async def test_calling_it_twice_still_cancels_once() -> None:
    started = asyncio.Event()
    task = asyncio.create_task(_main_waiting(started, calls=2))
    await started.wait()

    _raise(SHUTDOWN[-1])
    with pytest.raises(asyncio.CancelledError):
        await task

    assert task.cancelling() == 1


@pytest.mark.unit
async def test_the_handlers_stay_after_the_task_ends() -> None:
    for handled in SHUTDOWN:
        signal.signal(handled, _sentinel)

    async def main() -> None:
        handle_signals()

    await asyncio.create_task(main())

    for sig in SHUTDOWN:
        assert signal.getsignal(sig) not in (_sentinel, signal.SIG_DFL)


@pytest.mark.unit
def test_it_needs_a_task_to_cancel() -> None:
    with pytest.raises(RuntimeError):
        handle_signals()


@pytest.mark.unit
def test_importing_the_library_installs_no_handler() -> None:
    probe = "; ".join(
        [
            "import signal",
            f"sigs = {[int(s) for s in SHUTDOWN]}",
            "before = [signal.getsignal(s) for s in sigs]",
            "import waystation",
            "assert [signal.getsignal(s) for s in sigs] == before",
        ]
    )
    subprocess.run([sys.executable, "-c", probe], check=True)


@pytest.mark.git
async def test_a_run_installs_no_handler(host_repo: Path) -> None:
    before = [signal.getsignal(sig) for sig in SHUTDOWN]

    await a_run(host_repo)

    assert [signal.getsignal(sig) for sig in SHUTDOWN] == before


# A signal delivered to another process: POSIX only, since os.kill on Windows
# terminates rather than signals. The in-process tests above cover the
# Windows wiring.
posix_only = pytest.mark.skipif(
    sys.platform == "win32", reason="Windows cannot send SIGTERM to a process"
)

_FLOW = """
import asyncio, sys
sys.path.insert(0, {tests!r})
from helpers import ShellAgent
from waystation import Flow, NoSandbox, handle_signals

AGENT = ShellAgent({script!r})


def announce(ctx, line):
    if line.raw == "ready":
        print("ready", ctx.run_id, flush=True)


async def main():
    handle_signals()
    flow = Flow({repo!r}, agent=AGENT, sandbox=NoSandbox())
    await flow.run("work until stopped").on_agent_output(announce)


asyncio.run(main())
"""


def _spawn(script: str, tmp_path: Path, temp: Path) -> subprocess.Popen[str]:
    path = tmp_path / "flow.py"
    path.write_text(script, encoding="utf-8")
    return subprocess.Popen(
        [sys.executable, str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "TMPDIR": str(temp)},
    )


def _await_line(proc: subprocess.Popen[str], expected: str) -> str:
    assert proc.stdout is not None
    line: str = proc.stdout.readline()
    if not line.startswith(expected):
        proc.kill()
        _, err = proc.communicate()
        pytest.fail(f"expected {expected!r}, got {line!r}; stderr:\n{err}")
    return line


@posix_only
@pytest.mark.git
def test_sigterm_to_a_flow_script_keeps_the_series_and_leaks_no_sandbox(
    host_repo: Path, tmp_path: Path
) -> None:
    temp = tmp_path / "flow-temp"
    temp.mkdir()
    script = _FLOW.format(
        tests=str(Path(__file__).parent),
        script=WORKS_UNTIL_STOPPED,
        repo=str(host_repo),
    )
    proc = _spawn(script, tmp_path, temp)
    run_id = _await_line(proc, "ready").split()[1]

    proc.send_signal(signal.SIGTERM)
    _, err = proc.communicate(timeout=30)

    assert proc.returncode == 1, err
    assert "CancelledError: received SIGTERM" in err
    branch = f"waystation/{run_id}"
    kept = subjects(host_repo, f"HEAD..{branch}")
    assert kept == ["WIP: salvaged uncommitted work", "first"]
    assert workspaces(temp) == []


_WEDGED = """
import asyncio, time
from waystation import handle_signals


async def main():
    handle_signals()
    print("waiting", flush=True)
    try:
        await asyncio.sleep(60)
    except asyncio.CancelledError:
        print("cleaning up", flush=True)
        time.sleep(60)  # a cleanup that never ends


asyncio.run(main())
"""

_FINISHED = """
import asyncio, time
from waystation import handle_signals


async def main():
    handle_signals()


asyncio.run(main())
print("finished", flush=True)
time.sleep(60)
"""


@posix_only
@pytest.mark.unit
@pytest.mark.parametrize(
    ("script", "signals"),
    [
        pytest.param(_WEDGED, ["waiting", "cleaning up"], id="second-signal"),
        pytest.param(_FINISHED, ["finished"], id="after-the-task-ended"),
    ],
)
def test_a_signal_with_nothing_left_to_cancel_ends_the_process(
    script: str, signals: list[str], tmp_path: Path
) -> None:
    proc = _spawn(script, tmp_path, tmp_path)
    for expected in signals:
        _await_line(proc, expected)
        proc.send_signal(signal.SIGTERM)

    proc.communicate(timeout=30)

    assert proc.returncode == -signal.SIGTERM
