"""Marker-line Outcome helpers for agents without native schema output."""

from __future__ import annotations

import json
from typing import Any

OUTCOME_MARKER = "!?"


def outcome_instructions(schema: dict[str, Any]) -> str:
    """Prompt text telling an agent to emit a marker-prefixed Outcome line."""
    return (
        f"When finished, print a single line starting with '{OUTCOME_MARKER} ' "
        "followed by a JSON object matching this schema:\n"
        f"{json.dumps(schema)}"
    )


def find_outcome(text: str) -> Any | None:
    """Return the parsed JSON from the last marker-prefixed line, if any."""
    found: Any | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith(OUTCOME_MARKER):
            continue
        payload = stripped[len(OUTCOME_MARKER) :].strip()
        if not payload:
            continue
        try:
            found = json.loads(payload)
        except json.JSONDecodeError:
            continue
    return found
