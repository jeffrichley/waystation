"""Helpers every test module may reach for. Look here before writing your own.

Fixtures live in ``conftest.py``; this module holds the plain functions, so a
helper can be called from a fixture, a test body, or another helper.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from waystation import (
    Flow,
    NoSandbox,
    RunSpec,
    ScriptedAgent,
    ScriptedCommit,
    Summary,
)
from waystation.agents import (
    AgentCommand,
    AgentEvent,
    AgentText,
    OutcomeReported,
)

__all__ = [
    "OK_OUTCOME",
    "OK_OUTCOME_LINE",
    "OUTCOME",
    "PROMPT",
    "ShellAgent",
    "a_run",
    "commit_on",
    "git",
    "init_host_repo",
    "sh",
]

OK_OUTCOME = {"summary": "ok"}
"""The Outcome a scripted agent reports when the test doesn't care what it says."""

OUTCOME = "OUTCOME "
"""The marker line ``ShellAgent`` reports its Outcome on."""

OK_OUTCOME_LINE = OUTCOME + '{"summary": "ok"}'
"""A whole reporting line, for a script that only needs to finish cleanly."""

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


def commit_on(
    repo: Path,
    branch: str,
    files: Mapping[str, str],
    *,
    message: str | None = None,
) -> str:
    """Commit ``files`` on ``branch`` (made at HEAD if missing); return its tip.

    The checkout comes back to the branch it was on. Contents are written as
    bytes, so a line ending is exactly what the test wrote, on every host.
    """
    home = git(repo, "symbolic-ref", "--short", "HEAD")
    exists = git(repo, "branch", "--list", branch) != ""
    git(repo, "checkout", "-q", *(() if exists else ("-b",)), branch)
    for path, text in files.items():
        (repo / path).write_bytes(text.encode())
    git(repo, "add", *files)
    git(repo, "commit", "-q", "-m", message or f"outside: {', '.join(files)}")
    git(repo, "checkout", "-q", home)
    return git(repo, "rev-parse", branch)


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


def a_run(
    repo: Path,
    prompt: str | Path = PROMPT,
    *,
    commits: Sequence[ScriptedCommit] = (ScriptedCommit("add a file", {"a.txt": "x"}),),
) -> RunSpec[Summary]:
    """A run that says one line, makes ``commits`` (one, by default), and reports."""
    return Flow(
        repo,
        agent=ScriptedAgent(lines=["working"], outcome=OK_OUTCOME, commits=commits),
        sandbox=NoSandbox(),
    ).run(prompt)


@dataclass(frozen=True)
class ShellAgent:
    """An agent that is a shell script; an ``OUTCOME <json>`` line reports.

    Reach for this over ``ScriptedAgent`` when the test needs to control the
    script itself — writing to stderr, say, or exiting mid-stream.
    """

    script: str

    def preflight(self) -> None:
        return None

    def command(self, prompt: str, outcome_schema: dict[str, Any]) -> AgentCommand:
        return AgentCommand(argv=(sh(), "-c", self.script))

    def parse(self, line: str) -> Sequence[AgentEvent]:
        if line.startswith(OUTCOME):
            return (OutcomeReported(json.loads(line.removeprefix(OUTCOME))),)
        return (AgentText(line),) if line else ()
