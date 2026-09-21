"""The dump a wedged test leaves behind (#105).

`faulthandler` already dumps threads, and that is what proved the #105
failures were hangs rather than crashes. What it cannot show is the *await*:
a suspended coroutine sits on no thread stack, so a hung async test reports
an idle event loop and nothing else. These hold the part that fills that in.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import hang_dump


async def _dump_written(dumps: Path) -> str:
    """The dump file, once the timer has written one."""
    while not list(dumps.glob("*.txt")):
        await asyncio.sleep(0.02)
    (written,) = list(dumps.glob("*.txt"))
    return written.read_text(encoding="utf-8")


@pytest.mark.unit
async def test_a_wedged_test_leaves_a_dump_naming_the_await(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The thing #105 needed twice and did not have: which await never came back."""
    dumps = tmp_path / "dumps"
    monkeypatch.setattr(hang_dump, "DUMPS", dumps)
    wedged = asyncio.create_task(asyncio.Event().wait(), name="the-await-that-hung")
    timer = hang_dump.arm("tests/imaginary.py::test_that_hung", after=0.0)
    try:
        text = await _dump_written(dumps)
    finally:
        timer.cancel()
        wedged.cancel()

    assert "test_that_hung" in text, "the dump says which test was stuck"
    assert "the-await-that-hung" in text, "and names the task that never finished"
    # Innermost frame last, as a traceback reads: the last line is the await.
    suspended = text.split("the-await-that-hung")[1].splitlines()[1]
    assert suspended.strip().startswith('File "'), (
        f"a task is followed by the line it is suspended at, got {suspended!r}"
    )


@pytest.mark.unit
async def test_a_cancelled_timer_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A test that ends in time leaves no dump — every file here is a real hang."""
    dumps = tmp_path / "dumps"
    monkeypatch.setattr(hang_dump, "DUMPS", dumps)
    hang_dump.arm("tests/imaginary.py::test_that_finished", after=0.0).cancel()
    await asyncio.sleep(0.05)
    assert not dumps.exists()
