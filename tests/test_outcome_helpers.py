"""Unit tests for marker-line Outcome helpers."""

from __future__ import annotations

import pytest

from waystation.agents.outcome import OUTCOME_MARKER, find_outcome, outcome_instructions


@pytest.mark.unit
def test_find_outcome_returns_last_marker_json() -> None:
    text = f'noise\n{OUTCOME_MARKER} {{"a": 1}}\n{OUTCOME_MARKER} {{"a": 2}}\n'
    assert find_outcome(text) == {"a": 2}


@pytest.mark.unit
def test_outcome_instructions_mention_marker() -> None:
    text = outcome_instructions({"type": "object"})
    assert OUTCOME_MARKER in text
