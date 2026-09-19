"""ScriptedAgent plays back under the host's sh, or under the one it is given."""

from __future__ import annotations

from pathlib import Path

import pytest

from waystation import PreflightError, ScriptedAgent


@pytest.mark.unit
def test_a_scripted_agent_runs_under_the_sh_it_is_given() -> None:
    # A sandbox with its own sh has no use for the host's path to one.
    command = ScriptedAgent(lines=["hi"], shell="sh").command("", {})

    assert command.argv[:2] == ("sh", "-c")


@pytest.mark.unit
def test_a_scripted_agent_given_a_shell_needs_none_on_the_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))  # no sh, and no git to find one by

    ScriptedAgent(shell="sh").preflight()
    with pytest.raises(PreflightError, match="sh"):
        ScriptedAgent().preflight()
