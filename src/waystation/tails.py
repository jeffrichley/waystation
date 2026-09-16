"""Bounded output tails for exec results and failure diagnostics."""

from __future__ import annotations

from collections import deque

DEFAULT_MAX_LINES = 80
DEFAULT_MAX_BYTES = 2048


def bound_tail(
    text: str,
    *,
    max_lines: int = DEFAULT_MAX_LINES,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> str:
    """Last ``max_lines`` lines or ``max_bytes`` bytes, whichever is smaller."""
    if not text:
        return ""
    lines = text.splitlines(keepends=True)
    selected: deque[str] = deque()
    size = 0
    for line in reversed(lines):
        raw = line.encode("utf-8")
        if not selected and len(raw) > max_bytes:
            clipped = raw[-max_bytes:].decode("utf-8", errors="replace")
            return clipped
        if selected and (len(selected) >= max_lines or size + len(raw) > max_bytes):
            break
        selected.appendleft(line)
        size += len(raw)
        if len(selected) >= max_lines or size >= max_bytes:
            break
    return "".join(selected)


class TailBuffer:
    """Rolling buffer that never holds more than the tail limits."""

    def __init__(
        self,
        *,
        max_lines: int = DEFAULT_MAX_LINES,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        self._max_lines = max_lines
        self._max_bytes = max_bytes
        self._lines: deque[str] = deque()
        self._size = 0

    def append(self, chunk: str) -> None:
        if not chunk:
            return
        raw = chunk.encode("utf-8")
        self._lines.append(chunk)
        self._size += len(raw)
        self._trim()

    def _trim(self) -> None:
        while self._lines and (
            len(self._lines) > self._max_lines
            or (self._size > self._max_bytes and len(self._lines) > 1)
        ):
            removed = self._lines.popleft()
            self._size -= len(removed.encode("utf-8"))
        if self._lines and self._size > self._max_bytes:
            line = self._lines[0]
            raw = line.encode("utf-8")[-self._max_bytes :]
            clipped = raw.decode("utf-8", errors="replace")
            self._lines.clear()
            self._lines.append(clipped)
            self._size = len(raw)

    def text(self) -> str:
        return "".join(self._lines)
