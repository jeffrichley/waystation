"""Git-tier tracer: one outcome-only run on NoSandbox."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

import pytest
from pydantic import BaseModel

from waystation import (
    Flow,
    NoSandbox,
    RunSucceeded,
    ScriptedAgent,
    Summary,
    prepare_workspace,
)


@pytest.fixture
def host_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "host"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Waystation Test")
    _git(repo, "config", "user.email", "test@waystation.example")
    (repo / "README").write_text("committed\n", encoding="utf-8")
    _git(repo, "add", "README")
    _git(repo, "commit", "-m", "init")
    return repo


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


class Answer(BaseModel):
    summary: str


@dataclass(frozen=True)
class DcAnswer:
    summary: str


class TdAnswer(TypedDict):
    summary: str


@pytest.mark.git
@pytest.mark.asyncio
async def test_outcome_only_run_returns_validated_outcome(host_repo: Path) -> None:
    flow = Flow(
        host_repo,
        agent=ScriptedAgent(outcome=Answer(summary="done")),
        sandbox=NoSandbox(),
    )
    result = await flow.run("do the thing", outcome=Answer)

    assert isinstance(result, RunSucceeded)
    assert result.outcome == Answer(summary="done")
    assert result.base_sha is not None
    assert len(result.run_id) == 8
    assert all(c in "0123456789abcdef" for c in result.run_id)
    assert result.agent is not None
    assert result.agent.exit_code == 0
    assert result.agent.hanging is False
    assert "workspace" in result.elapsed
    assert "agent" in result.elapsed


@pytest.mark.git
@pytest.mark.asyncio
async def test_awaiting_same_spec_twice_yields_distinct_run_ids(
    host_repo: Path,
) -> None:
    flow = Flow(
        host_repo,
        agent=ScriptedAgent(outcome=Answer(summary="once")),
        sandbox=NoSandbox(),
    )
    spec = flow.run("again", outcome=Answer)
    first = await spec
    second = await spec
    assert first.run_id != second.run_id
    assert len(first.run_id) == 8
    assert len(second.run_id) == 8


@pytest.mark.git
@pytest.mark.asyncio
async def test_outcome_accepts_dataclass_and_typeddict(host_repo: Path) -> None:
    flow = Flow(
        host_repo,
        agent=ScriptedAgent(outcome=DcAnswer(summary="dc")),
        sandbox=NoSandbox(),
    )
    dc_result = await flow.run("dc", outcome=DcAnswer)
    assert dc_result.outcome == DcAnswer(summary="dc")

    flow_td = Flow(
        host_repo,
        agent=ScriptedAgent(outcome={"summary": "td"}),
        sandbox=NoSandbox(),
    )
    td_result = await flow_td.run("td", outcome=TdAnswer)
    assert td_result.outcome == {"summary": "td"}


@pytest.mark.git
def test_non_object_outcome_raises_at_flow_run(host_repo: Path) -> None:
    flow = Flow(
        host_repo,
        agent=ScriptedAgent(outcome="nope"),
        sandbox=NoSandbox(),
    )
    with pytest.raises(TypeError, match="object-shaped"):
        flow.run("x", outcome=str)


@pytest.mark.git
@pytest.mark.asyncio
async def test_default_outcome_is_summary(host_repo: Path) -> None:
    flow = Flow(
        host_repo,
        agent=ScriptedAgent(outcome=Summary(summary="default")),
        sandbox=NoSandbox(),
    )
    result = await flow.run("use default")
    assert isinstance(result.outcome, Summary)
    assert result.outcome.summary == "default"


@pytest.mark.git
@pytest.mark.asyncio
async def test_path_prompt_is_read_at_agent_stage(
    host_repo: Path, tmp_path: Path
) -> None:
    from waystation import Errored, RunFailed

    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("early", encoding="utf-8")
    flow = Flow(
        host_repo,
        agent=ScriptedAgent(outcome=Answer(summary="ok")),
        sandbox=NoSandbox(),
    )
    spec = flow.run(prompt_file, outcome=Answer)
    prompt_file.unlink()
    result = await spec
    assert isinstance(result, RunFailed)
    assert result.stage == "agent"
    assert isinstance(result.failure, Errored)
    assert isinstance(result.failure.exception, FileNotFoundError)


@pytest.mark.git
@pytest.mark.asyncio
async def test_uncommitted_host_changes_are_invisible(host_repo: Path) -> None:
    (host_repo / "SECRET").write_text("leak\n", encoding="utf-8")
    ws = prepare_workspace(host_repo)
    try:
        assert not (ws.path / "SECRET").exists()
        assert (ws.path / "README").read_text(encoding="utf-8") == "committed\n"
    finally:
        import shutil

        shutil.rmtree(ws.path, ignore_errors=True)


@pytest.mark.git
@pytest.mark.asyncio
async def test_base_ref_resolved_when_await_starts(host_repo: Path) -> None:
    flow = Flow(
        host_repo,
        agent=ScriptedAgent(outcome=Answer(summary="later")),
        sandbox=NoSandbox(),
    )
    spec = flow.run("deferred", outcome=Answer)
    (host_repo / "README").write_text("committed\nupdated\n", encoding="utf-8")
    _git(host_repo, "add", "README")
    _git(host_repo, "commit", "-m", "after run built")
    new_sha = _git(host_repo, "rev-parse", "HEAD")

    result = await spec
    assert result.base_sha == new_sha


@pytest.mark.git
def test_prepare_workspace_checks_out_waystation_branch(host_repo: Path) -> None:
    ws = prepare_workspace(host_repo)
    try:
        branch = _git(ws.path, "branch", "--show-current")
        assert branch == f"waystation/{ws.run_id}"
        assert (ws.path / "README").read_text(encoding="utf-8") == "committed\n"
        assert not (ws.path / "SECRET").exists()
    finally:
        import shutil

        shutil.rmtree(ws.path, ignore_errors=True)
