"""Tier markers are expressible and selected correctly by default."""

from __future__ import annotations

import shutil
import subprocess

import pytest


@pytest.mark.unit
def test_unit_tier_runs() -> None:
    assert True


@pytest.mark.git
def test_git_tier_runs() -> None:
    git = shutil.which("git")
    assert git is not None
    result = subprocess.run(
        [git, "--version"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "git version" in result.stdout.lower()


@pytest.mark.docker
def test_docker_tier_pings_daemon() -> None:
    docker = shutil.which("docker")
    assert docker is not None
    result = subprocess.run(
        [docker, "version"],
        check=True,
        capture_output=True,
    )
    assert result.returncode == 0


@pytest.mark.live
def test_live_tier_only_when_selected() -> None:
    """Selected only with ``pytest -m live``; default addopts exclude it."""
    assert True
