"""ScriptedAgent hands back a script; the sandbox says which shell (ADR-0036)."""

from __future__ import annotations

from pathlib import Path

import pytest

from waystation import AgentCommand, ScriptedAgent


@pytest.mark.unit
def test_a_scripted_agent_hands_back_a_script_rather_than_a_command_line() -> None:
    # The provider cannot know whether its sandbox is this host or a
    # container, so it never spells the shell (#77).
    command = ScriptedAgent(lines=["hi"]).command("", {})

    assert command.argv == ()
    assert command.script is not None
    assert "printf '%s\\n' 'hi'" in command.script


@pytest.mark.unit
def test_a_scripted_agents_script_runs_under_whatever_shell_it_is_given() -> None:
    command = ScriptedAgent(lines=["hi"]).command("", {})

    assert command.argv_in(("sh", "-c")) == ("sh", "-c", command.script)
    assert command.argv_in(("C:/Git/bin/sh.exe", "-c"))[0] == "C:/Git/bin/sh.exe"


@pytest.mark.unit
def test_a_scripted_agent_needs_nothing_of_the_host_at_preflight(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The shell it needs is the sandbox's, and the sandbox is what checks it.

    It used to look for the host's sh here, which is the check that made a
    provider care which backend it was about to run on.
    """
    monkeypatch.setenv("PATH", str(tmp_path))  # no sh, and no git to find one by

    ScriptedAgent().preflight()


@pytest.mark.unit
def test_a_command_runs_an_argv_or_a_script_and_says_so_when_it_names_neither() -> None:
    with pytest.raises(ValueError, match="neither"):
        AgentCommand()
    with pytest.raises(ValueError, match="both"):
        AgentCommand(argv=("sh",), script="echo hi")
