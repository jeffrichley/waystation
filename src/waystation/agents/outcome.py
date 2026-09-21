"""Marker-line Outcome helpers for agents without native schema output.

A provider whose agent has no ``--json-schema`` of its own appends
``outcome_instructions(schema)`` to the prompt and calls ``find_outcome`` from
``parse`` (ADR-0019). ``ScriptedAgent`` reads its playback through the same
``find_outcome``, so there is one marker parser, not two that disagree (#43).
"""

from __future__ import annotations

import json
from typing import Any

from waystation.agents.protocol import OutcomeReported

__all__ = ["OUTCOME_MARKER", "find_outcome", "outcome_instructions"]

OUTCOME_MARKER = "!?"


def outcome_instructions(schema: dict[str, Any]) -> str:
    """Prompt text telling an agent to end with a marker-prefixed Outcome line.

    Args:
        schema: The run's Outcome JSON Schema, as ``run_agent`` hands it to
            ``AgentProvider.command``.

    Returns:
        Text to append to the prompt.
    """
    return (
        f"When finished, print a single line starting with '{OUTCOME_MARKER} ' "
        "followed by a JSON object matching this schema:\n"
        f"{json.dumps(schema)}"
    )


def find_outcome(text: str) -> OutcomeReported | None:
    """The report the last marker line in ``text`` makes, or ``None`` for none.

    Scans from the end, so chatter the agent prints after its Outcome is
    ignored. A marker whose payload is not JSON is still a report, of its raw
    text, and a marker holding JSON ``null`` is a report of ``None``: core then
    returns ``OutcomeInvalid`` for either, where swallowing them would make it
    ``OutcomeMissing`` and hide what the agent actually said (ADR-0019). A
    bare marker with no payload is not a report.

    Args:
        text: Agent output: one line from ``parse``, or a whole transcript.

    Returns:
        ``OutcomeReported`` to yield from ``parse``, or ``None`` when ``text``
        holds no marker line.
    """
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if not stripped.startswith(OUTCOME_MARKER):
            continue
        payload = stripped[len(OUTCOME_MARKER) :].strip()
        if not payload:
            continue
        try:
            return OutcomeReported(json.loads(payload))
        except json.JSONDecodeError:
            return OutcomeReported(payload)
    return None
