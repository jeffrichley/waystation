"""What a workspace holds: its own branch, and nothing else of the host's."""

from __future__ import annotations

from pathlib import Path

import pytest

from helpers import commit_on, git
from waystation import prepare_workspace


@pytest.mark.git
async def test_a_workspace_holds_only_the_refs_that_travel(host_repo: Path) -> None:
    """A clone brings every host ref along — other branches, and tags.

    The copy transport bundles one branch, so a bind or a no-sandbox run
    would otherwise show the agent a different repository than a container
    run of the same flow.
    """
    commit_on(host_repo, "other", {"o.txt": "o\n"})
    git(host_repo, "tag", "v1")

    workspace = await prepare_workspace(host_repo)

    listed = git(workspace.path, "for-each-ref", "--format=%(refname)").splitlines()
    assert listed == [f"refs/heads/{workspace.branch}"]
    assert workspace.refs == (f"refs/heads/{workspace.branch}",)


@pytest.mark.git
async def test_a_workspace_cannot_reach_the_host_it_was_cloned_from(
    host_repo: Path,
) -> None:
    """``origin`` would be the host path, and under NoSandbox the agent could push."""
    workspace = await prepare_workspace(host_repo)

    assert git(workspace.path, "remote") == ""


@pytest.mark.git
async def test_a_workspace_is_checked_out_on_the_branch_it_names(
    host_repo: Path,
) -> None:
    """``branch`` is the name, derived once here rather than re-spelled downstream."""
    workspace = await prepare_workspace(host_repo)

    assert git(workspace.path, "symbolic-ref", "--short", "HEAD") == workspace.branch
    assert git(workspace.path, "rev-parse", "HEAD") == workspace.base_sha


@pytest.mark.git
async def test_a_workspace_keeps_the_history_its_base_reaches(host_repo: Path) -> None:
    """Stripping the host's refs must not strip the commits the agent works from."""
    second = commit_on(host_repo, "side", {"s.txt": "s\n"})

    workspace = await prepare_workspace(host_repo, base=second)

    assert git(workspace.path, "cat-file", "-t", second) == "commit"
    assert git(workspace.path, "rev-parse", "HEAD") == second
