"""Named-branch integration via public run API (issue #23)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from pydantic import BaseModel

from waystation import (
    Flow,
    Integration,
    NoSandbox,
    RunSucceeded,
    ScriptedAgent,
    ScriptedCommit,
)


class Answer(BaseModel):
    summary: str


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


@pytest.mark.git
@pytest.mark.asyncio
async def test_integrate_apply_onto_new_named_branch(host_repo: Path) -> None:
    head_before = _git(host_repo, "rev-parse", "HEAD")
    flow = Flow(
        host_repo,
        agent=ScriptedAgent(
            commits=(ScriptedCommit(message="add feat", files={"feat.txt": "x\n"}),),
            outcome=Answer(summary="ok"),
        ),
        sandbox=NoSandbox(),
        integration=Integration("agents/demo"),
    )
    result = await flow.run("land it", outcome=Answer)

    assert isinstance(result, RunSucceeded)
    assert result.preserved is None
    assert result.series is not None
    assert result.series.commits == 1
    assert result.report is not None
    assert result.report.target == "agents/demo"
    assert result.report.mechanism == "apply"
    assert result.report.target_before == head_before  # created at base
    assert result.report.target_after is not None
    assert len(result.report.landed) == 1
    assert _git(host_repo, "rev-parse", "HEAD") == head_before
    assert _git(host_repo, "status", "--porcelain") == ""
    assert "x" in _git(host_repo, "show", "agents/demo:feat.txt")
    assert _git(host_repo, "log", "-1", "--format=%s", "agents/demo") == "add feat"
    # no preservation branch
    refs = _git(host_repo, "for-each-ref", "--format=%(refname:short)")
    assert f"waystation/{result.run_id}" not in refs.splitlines()


@pytest.mark.git
@pytest.mark.asyncio
async def test_integrate_merge_onto_existing_branch(host_repo: Path) -> None:
    _git(host_repo, "branch", "agents/batch")
    before = _git(host_repo, "rev-parse", "agents/batch")
    flow = Flow(
        host_repo,
        agent=ScriptedAgent(
            commits=(ScriptedCommit(message="batch work", files={"b.txt": "b\n"}),),
            outcome=Answer(summary="merged"),
        ),
        sandbox=NoSandbox(),
    )
    result = await flow.run("merge land", outcome=Answer).integrate(
        "agents/batch", mechanism="merge"
    )
    assert isinstance(result, RunSucceeded)
    assert result.report is not None
    assert result.report.mechanism == "merge"
    assert result.report.target_before == before
    assert result.preserved is None
    # merge commit has two parents when tip moved
    parents = _git(host_repo, "rev-list", "--parents", "-n", "1", "agents/batch")
    assert len(parents.split()) >= 2
    assert "b" in _git(host_repo, "show", "agents/batch:b.txt")
    assert _git(host_repo, "rev-parse", "HEAD") == _git(host_repo, "rev-parse", before)


@pytest.mark.git
@pytest.mark.asyncio
async def test_integrate_none_clears_flow_default(host_repo: Path) -> None:
    flow = Flow(
        host_repo,
        agent=ScriptedAgent(
            commits=(ScriptedCommit(message="park", files={"p.txt": "p\n"}),),
            outcome=Answer(summary="ok"),
        ),
        sandbox=NoSandbox(),
        integration=Integration("agents/default"),
    )
    result = await flow.run("opt out", outcome=Answer).integrate(None)
    assert isinstance(result, RunSucceeded)
    assert result.report is None
    assert result.preserved == f"waystation/{result.run_id}"
    refs = _git(host_repo, "for-each-ref", "--format=%(refname:short)")
    assert "agents/default" not in refs.splitlines()


@pytest.mark.git
@pytest.mark.asyncio
async def test_integrate_primitive_from_range(host_repo: Path) -> None:
    from waystation import PatchSeries, integrate

    _git(host_repo, "checkout", "-b", "feature")
    (host_repo / "f.txt").write_text("f\n", encoding="utf-8")
    _git(host_repo, "add", "f.txt")
    _git(host_repo, "commit", "-m", "on feature")
    base = _git(host_repo, "rev-parse", "feature^")
    _git(host_repo, "checkout", "-")
    series = PatchSeries.from_range(host_repo, base, "feature")
    report = await integrate(host_repo, series, Integration("agents/from-range"))
    assert report.target == "agents/from-range"
    assert len(report.landed) == 1
    assert "f" in _git(host_repo, "show", "agents/from-range:f.txt")


@pytest.mark.git
@pytest.mark.asyncio
async def test_integrate_serializes_concurrent_lands(host_repo: Path) -> None:
    import asyncio

    from waystation import PatchSeries, integrate

    base = _git(host_repo, "rev-parse", "HEAD")

    _git(host_repo, "checkout", "-b", "side-a")
    (host_repo / "a.txt").write_text("a\n", encoding="utf-8")
    _git(host_repo, "add", "a.txt")
    _git(host_repo, "commit", "-m", "a")
    series_a = PatchSeries.from_range(host_repo, base, "side-a")

    _git(host_repo, "checkout", base)
    _git(host_repo, "checkout", "-b", "side-b")
    (host_repo / "b.txt").write_text("b\n", encoding="utf-8")
    _git(host_repo, "add", "b.txt")
    _git(host_repo, "commit", "-m", "b")
    series_b = PatchSeries.from_range(host_repo, base, "side-b")
    _git(host_repo, "checkout", base)

    r1, r2 = await asyncio.gather(
        integrate(host_repo, series_a, Integration("agents/race")),
        integrate(host_repo, series_b, Integration("agents/race")),
    )
    assert r1.target_after is not None
    assert r2.target_after is not None
    tip = _git(host_repo, "rev-parse", "agents/race")
    assert tip in {r1.target_after, r2.target_after}
    tree_files = _git(host_repo, "ls-tree", "--name-only", "-r", "agents/race")
    assert "a.txt" in tree_files
    assert "b.txt" in tree_files
