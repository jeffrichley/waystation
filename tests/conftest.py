"""Shared pytest fixtures and tier hooks."""

from __future__ import annotations

import functools
import logging
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from helpers import init_host_repo


@pytest.fixture(autouse=True)
def isolated_tempdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Give each test its own temp dir, so workspaces never land in the host's."""
    temp = tmp_path / "temp"
    temp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp))
    return temp


@pytest.fixture
def host_repo(tmp_path: Path) -> Path:
    """A throwaway host repo with a git identity and one commit on HEAD."""
    return init_host_repo(tmp_path)


@pytest.fixture
def clean_logging() -> Iterator[None]:
    """Restore the ``waystation`` loggers after a test configures or tunes them.

    Levels come back across the whole subtree, not just the package logger: a
    test that turns one stream up — ``waystation.agent.output``, say — would
    otherwise leave it up for whatever runs next in that worker, and the next
    test's idea of what a host collects would be someone else's.
    """
    logger = logging.getLogger("waystation")
    handlers = list(logger.handlers)
    propagate = logger.propagate  # a run file turns it off while one is open
    levels = _waystation_levels()
    try:
        yield
    finally:
        logger.handlers[:] = handlers
        logger.propagate = propagate
        for name, existing in _waystation_loggers():
            existing.setLevel(levels.get(name, logging.NOTSET))


def _waystation_loggers() -> list[tuple[str, logging.Logger]]:
    """Every ``waystation`` logger that exists right now, package logger included."""
    known = logging.getLogger().manager.loggerDict
    return [
        (name, found)
        for name, found in list(known.items())
        if (name == "waystation" or name.startswith("waystation."))
        and isinstance(found, logging.Logger)
    ]


def _waystation_levels() -> dict[str, int]:
    return {name: found.level for name, found in _waystation_loggers()}


@functools.cache
def _no_linux_docker() -> str | None:
    """Why the docker tier cannot run here, or ``None`` when it can.

    Asked once per worker. The tier's image is Linux, so a daemon running
    Windows containers cannot host it either. The reason says which it was.
    """
    docker = shutil.which("docker")
    if docker is None:
        return "Docker daemon not reachable: no docker CLI on PATH"
    try:
        result = subprocess.run(
            [docker, "info", "--format", "{{.OSType}}"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        return "Docker daemon not reachable: `docker info` gave no answer in 10 s"
    except OSError as exc:
        return f"Docker daemon not reachable: {exc}"
    if result.returncode != 0:
        said = result.stderr.strip().splitlines() or [f"exit {result.returncode}"]
        return f"Docker daemon not reachable: {said[-1]}"
    os_type = result.stdout.strip()
    if os_type != "linux":
        return f"Docker daemon runs {os_type} containers; the docker tier needs linux"
    return None


def pytest_runtest_setup(item: pytest.Item) -> None:
    if item.get_closest_marker("docker") is not None:
        reason = _no_linux_docker()
        if reason is not None:
            pytest.skip(reason)
