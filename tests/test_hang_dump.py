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


async def _wedges_deep(gate: asyncio.Event) -> None:
    await _one_frame_further(gate)


async def _one_frame_further(gate: asyncio.Event) -> None:
    await gate.wait()  # the await that never comes back


@pytest.mark.unit
async def test_a_wedged_test_leaves_a_dump_naming_the_await(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The thing #105 needed twice and did not have: which await never came back.

    The wedge is two frames deep inside one task on purpose. ``Task.get_stack``
    returns a single frame for a suspended task, so a dump built on it names
    ``_wedges_deep`` — the coroutine the task was made with, which is the one
    thing already known — and never reaches ``gate.wait()``.
    """
    dumps = tmp_path / "dumps"
    monkeypatch.setattr(hang_dump, "DUMPS", dumps)
    gate = asyncio.Event()
    wedged = asyncio.create_task(_wedges_deep(gate), name="the-await-that-hung")
    timer = hang_dump.arm("tests/imaginary.py::test_that_hung", after=0.0)
    try:
        text = await _dump_written(dumps)
    finally:
        timer.cancel()
        wedged.cancel()

    assert "test_that_hung" in text, "the dump says which test was stuck"
    assert "the-await-that-hung" in text, "and names the task that never finished"
    named = text.split("the-await-that-hung")[1]
    assert "_wedges_deep" in named, "the task's own frame"
    assert "_one_frame_further" in named, "and every frame it awaited through"
    assert "await gate.wait()" in named, (
        f"the innermost await is the point of the dump, got:\n{named}"
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
