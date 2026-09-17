"""Shared pytest fixtures and tier hooks."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_tempdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Give each test its own temp dir, so workspaces never land in the host's."""
    temp = tmp_path / "temp"
    temp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp))
    return temp


def _docker_daemon_reachable() -> bool:
    docker = shutil.which("docker")
    if docker is None:
        return False
    try:
        result = subprocess.run(
            [docker, "info"],
            check=False,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def pytest_runtest_setup(item: pytest.Item) -> None:
    if item.get_closest_marker("docker") is not None and not _docker_daemon_reachable():
        pytest.skip("Docker daemon not reachable")
