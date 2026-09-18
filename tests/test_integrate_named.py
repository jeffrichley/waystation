"""Named-branch integration via public run API (issue #23)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel

from helpers import commit_on, git
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


@pytest.mark.git
@pytest.mark.asyncio
async def test_integrate_apply_onto_new_named_branch(host_repo: Path) -> None:
    head_before = git(host_repo, "rev-parse", "HEAD")
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
    assert git(host_repo, "rev-parse", "HEAD") == head_before
    assert git(host_repo, "status", "--porcelain") == ""
    assert "x" in git(host_repo, "show", "agents/demo:feat.txt")
    assert git(host_repo, "log", "-1", "--format=%s", "agents/demo") == "add feat"
    # no preservation branch
    refs = git(host_repo, "for-each-ref", "--format=%(refname:short)")
    assert f"waystation/{result.run_id}" not in refs.splitlines()


@pytest.mark.git
@pytest.mark.asyncio
async def test_integrate_merge_onto_existing_branch(host_repo: Path) -> None:
    git(host_repo, "branch", "agents/batch")
    before = git(host_repo, "rev-parse", "agents/batch")
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
    parents = git(host_repo, "rev-list", "--parents", "-n", "1", "agents/batch")
    assert len(parents.split()) >= 2
    assert "b" in git(host_repo, "show", "agents/batch:b.txt")
    assert git(host_repo, "rev-parse", "HEAD") == git(host_repo, "rev-parse", before)


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
    refs = git(host_repo, "for-each-ref", "--format=%(refname:short)")
    assert "agents/default" not in refs.splitlines()


@pytest.mark.git
@pytest.mark.asyncio
async def test_integrate_primitive_from_range(host_repo: Path) -> None:
    from waystation import PatchSeries, integrate

    base = git(host_repo, "rev-parse", "HEAD")
    commit_on(host_repo, "feature", {"f.txt": "f\n"})
    series = await PatchSeries.from_range(host_repo, base, "feature")
    report = await integrate(host_repo, series, Integration("agents/from-range"))
    assert report.target == "agents/from-range"
    assert len(report.landed) == 1
    assert "f" in git(host_repo, "show", "agents/from-range:f.txt")


@pytest.mark.git
@pytest.mark.asyncio
async def test_integrate_serializes_concurrent_lands(host_repo: Path) -> None:
    import asyncio

    from waystation import PatchSeries, integrate

    base = git(host_repo, "rev-parse", "HEAD")

    commit_on(host_repo, "side-a", {"a.txt": "a\n"})
    commit_on(host_repo, "side-b", {"b.txt": "b\n"})
    series_a = await PatchSeries.from_range(host_repo, base, "side-a")
    series_b = await PatchSeries.from_range(host_repo, base, "side-b")

    r1, r2 = await asyncio.gather(
        integrate(host_repo, series_a, Integration("agents/race")),
        integrate(host_repo, series_b, Integration("agents/race")),
    )
    assert r1.target_after is not None
    assert r2.target_after is not None
    tip = git(host_repo, "rev-parse", "agents/race")
    assert tip in {r1.target_after, r2.target_after}
    tree_files = git(host_repo, "ls-tree", "--name-only", "-r", "agents/race")
    assert "a.txt" in tree_files
    assert "b.txt" in tree_files
