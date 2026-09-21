"""Unit tests for marker-line Outcome helpers (ADR-0019, #43).

``find_outcome`` is what a provider without native schema output calls from
``parse``. It returns the report to hand core, or ``None`` when there is no
marker at all — so a marker holding JSON ``null`` and a malformed marker are
both still reports, and core answers them with ``OutcomeInvalid``, never
``OutcomeMissing`` (#18 story 23).
"""

from __future__ import annotations

import pytest

from waystation import (
    OutcomeReported,
    ScriptedAgent,
    find_outcome,
    outcome_instructions,
)
from waystation.agents.outcome import OUTCOME_MARKER


@pytest.mark.unit
def test_the_last_marker_line_wins() -> None:
    text = f'noise\n{OUTCOME_MARKER} {{"a": 1}}\n{OUTCOME_MARKER} {{"a": 2}}\n'
    assert find_outcome(text) == OutcomeReported({"a": 2})


@pytest.mark.unit
def test_chatter_after_the_marker_line_is_tolerated() -> None:
    text = f'{OUTCOME_MARKER} {{"a": 1}}\nAll done! Let me know if you need more.\n'
    assert find_outcome(text) == OutcomeReported({"a": 1})


@pytest.mark.unit
def test_a_marker_may_be_indented() -> None:
    assert find_outcome(f'   {OUTCOME_MARKER} {{"a": 1}}') == OutcomeReported({"a": 1})


@pytest.mark.unit
def test_no_marker_is_no_report() -> None:
    assert find_outcome("just chatter\nand more\n") is None


@pytest.mark.unit
def test_a_bare_marker_with_no_payload_is_no_report() -> None:
    assert find_outcome(f"{OUTCOME_MARKER}\n{OUTCOME_MARKER}   \n") is None


@pytest.mark.unit
def test_a_marker_holding_null_is_a_report_of_null() -> None:
    """Distinguishable from no marker: core then says the report was invalid."""
    assert find_outcome(f"{OUTCOME_MARKER} null") == OutcomeReported(None)


@pytest.mark.unit
def test_a_marker_that_is_not_json_is_reported_as_its_raw_text() -> None:
    """A broken Outcome is still an Outcome, so core returns OutcomeInvalid."""
    text = f'{OUTCOME_MARKER} {{"a": 1}}\n{OUTCOME_MARKER} {{"a": oops\n'
    assert find_outcome(text) == OutcomeReported('{"a": oops')


@pytest.mark.unit
def test_scripted_agent_and_find_outcome_read_a_line_the_same_way() -> None:
    """One marker parser: the provider-author helper and ScriptedAgent agree."""
    agent = ScriptedAgent()
    for line in (
        f'{OUTCOME_MARKER} {{"a": 1}}',
        f"{OUTCOME_MARKER} null",
        f"{OUTCOME_MARKER} not-json",
        f"  {OUTCOME_MARKER} [1, 2]",
    ):
        found = find_outcome(line)
        assert found is not None
        assert list(agent.parse(line)) == [found], line


@pytest.mark.unit
def test_outcome_instructions_name_the_marker_and_the_schema() -> None:
    text = outcome_instructions({"type": "object", "title": "Verdict"})
    assert OUTCOME_MARKER in text
    assert '"title": "Verdict"' in text
