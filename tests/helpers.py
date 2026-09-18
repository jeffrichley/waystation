"""Helpers every test module may reach for. Look here before writing your own.

Fixtures live in ``conftest.py``; this module holds the plain functions, so a
helper can be called from a fixture, a test body, or another helper.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from waystation import (
    Flow,
    NoSandbox,
    RunSpec,
    ScriptedAgent,
    ScriptedCommit,
    Summary,
)

__all__ = ["OK_OUTCOME", "PROMPT", "a_run", "git", "init_host_repo", "sh"]

OK_OUTCOME = {"summary": "ok"}
"""The Outcome a scripted agent reports when the test doesn't care what it says."""

PROMPT = "Do the thing.\nWith detail on a second line."
"""A prompt with a second line, so a test can prove the body stayed unlogged."""


def git(repo: Path, *args: str) -> str:
    """Run git in ``repo``, return its stdout stripped, raise on a non-zero exit."""
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def init_host_repo(root: Path) -> Path:
    """Make a host repo under ``root``: a git identity and one commit on HEAD.

    Prefer the ``host_repo`` fixture; call this directly only when a test needs
    a second repo, or one somewhere other than ``tmp_path``.
    """
    repo = root / "host"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.name", "Waystation Test")
    git(repo, "config", "user.email", "test@waystation.example")
    (repo / "README").write_text("committed\n", encoding="utf-8")
    git(repo, "add", "README")
    git(repo, "commit", "-m", "init")
    return repo


def sh() -> str:
    """The POSIX sh ``ScriptedAgent`` found on this host (Git Bash on Windows)."""
    return str(ScriptedAgent().command("", {}).argv[0])


def a_run(repo: Path, prompt: str | Path = PROMPT) -> RunSpec[Summary]:
    """A run that says one line, makes one commit, and reports an Outcome."""
    return Flow(
        repo,
        agent=ScriptedAgent(
            lines=["working"],
            outcome=OK_OUTCOME,
            commits=[ScriptedCommit("add a file", {"a.txt": "x"})],
        ),
        sandbox=NoSandbox(),
    ).run(prompt)
