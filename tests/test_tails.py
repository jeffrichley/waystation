"""Unit tests for bounded output tails."""

from __future__ import annotations

import pytest

from waystation.tails import TailBuffer, bound_tail


@pytest.mark.unit
def test_bound_tail_prefers_smaller_of_lines_and_bytes() -> None:
    lines = [f"line-{i}\n" for i in range(100)]
    text = "".join(lines)
    out = bound_tail(text, max_lines=80, max_bytes=2048)
    assert out.count("\n") <= 80
    assert len(out.encode("utf-8")) <= 2048
    assert out.endswith("line-99\n")
    assert "line-0\n" not in out


@pytest.mark.unit
def test_bound_tail_byte_limit_clips_oversized_line() -> None:
    huge = "x" * 5000
    out = bound_tail(huge, max_lines=80, max_bytes=2048)
    assert len(out.encode("utf-8")) == 2048


@pytest.mark.unit
def test_tail_buffer_rolls_under_limits() -> None:
    buf = TailBuffer(max_lines=3, max_bytes=100)
    for i in range(10):
        buf.append(f"{i}\n")
    text = buf.text()
    assert text == "7\n8\n9\n"
    assert len(text.encode("utf-8")) <= 100
