"""Coverage for edge paths on public helpers and primitives."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from waystation import Flow, NoSandbox, ScriptedAgent, Summary, prepare_workspace
from waystation.agents.outcome import OUTCOME_MARKER, find_outcome


@pytest.mark.unit
def test_find_outcome_skips_empty_and_invalid_marker_lines() -> None:
    text = "\n".join(
        [
            OUTCOME_MARKER,
            f"{OUTCOME_MARKER} not-json",
            f'{OUTCOME_MARKER} {{"ok": true}}',
            "",
        ]
    )
    assert find_outcome(text) == {"ok": True}


@pytest.mark.git
async def test_prepare_workspace_requires_existing_repo(tmp_path: Path) -> None:
    missing = tmp_path / "nope"
    with pytest.raises(FileNotFoundError):
        await prepare_workspace(missing)


@pytest.mark.git
async def test_prepare_workspace_requires_git_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    repo = tmp_path / "bare"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    (repo / "f").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "f"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=T", "-c", "user.email=t@e.com", "commit", "-m", "i"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    from waystation import Refused, StageError

    with pytest.raises(StageError) as caught:
        await prepare_workspace(repo)
    assert isinstance(caught.value.failure, Refused)
    assert caught.value.failure.reason == "no_git_identity"


@pytest.mark.git
@pytest.mark.asyncio
async def test_run_returns_outcome_missing_when_no_report(tmp_path: Path) -> None:
    repo = tmp_path / "host"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.name", "T"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "t@e.com"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    (repo / "README").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "README"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "i"],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    from waystation import OutcomeMissing, RunFailed

    flow = Flow(
        repo,
        agent=ScriptedAgent(lines=["hello"], outcome=None),
        sandbox=NoSandbox(),
    )
    result = await flow.run("missing")
    assert isinstance(result, RunFailed)
    assert isinstance(result.failure, OutcomeMissing)


@pytest.mark.unit
def test_scripted_agent_serializes_mapping_and_lines() -> None:
    agent = ScriptedAgent(lines=["hi"], outcome={"summary": "m"})
    cmd = agent.command("p", {"type": "object"})
    assert cmd.script is not None
    assert "hi" in cmd.script
    assert OUTCOME_MARKER in cmd.script
    events = agent.parse(f'{OUTCOME_MARKER} {{"summary": "m"}}')
    assert len(events) == 1


@pytest.mark.git
@pytest.mark.asyncio
async def test_pass_env_reaches_agent_command(tmp_path: Path) -> None:
    repo = tmp_path / "host"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.name", "T"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "t@e.com"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    (repo / "README").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "README"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "i"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    os.environ["WAYSTATION_TEST_PASS"] = "visible"
    try:
        flow = Flow(
            repo,
            agent=ScriptedAgent(
                outcome={"summary": "ok"},
                pass_env=("WAYSTATION_TEST_PASS",),
            ),
            sandbox=NoSandbox(),
        )
        result = await flow.run("pass", outcome=Summary)
        assert result.outcome.summary == "ok"
    finally:
        os.environ.pop("WAYSTATION_TEST_PASS", None)
