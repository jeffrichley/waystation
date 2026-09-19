"""Helpers every test module may reach for. Look here before writing your own.

Fixtures live in ``conftest.py``; this module holds the plain functions, so a
helper can be called from a fixture, a test body, or another helper.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from waystation import (
    Flow,
    NoSandbox,
    RunResult,
    RunSpec,
    SandboxBackend,
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
    "awaited",
    "commit_on",
    "git",
    "host_state",
    "init_host_repo",
    "lifecycle",
    "sh",
    "stalling_ref_hook",
    "subjects",
    "until",
    "workspaces",
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


def subjects(repo: Path, revisions: str) -> list[str]:
    """The subject of each commit in ``revisions`` (``base..branch``), newest first."""
    return git(repo, "log", "--format=%s", revisions).splitlines()


def host_state(repo: Path) -> dict[str, object]:
    """What a landing must never touch: refs, HEAD, the index and the tree."""
    return {
        "refs": git(repo, "for-each-ref", "--format=%(refname) %(objectname)"),
        "head": git(repo, "symbolic-ref", "HEAD"),
        "index": (repo / ".git" / "index").read_bytes(),
        "tree": {
            path.relative_to(repo).as_posix(): path.read_bytes()
            for path in repo.rglob("*")
            if path.is_file() and path.relative_to(repo).parts[0] != ".git"
        },
    }


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


def workspaces(temp: Path) -> list[Path]:
    """The run workspaces under ``temp`` — ``[]`` once every run cleaned up."""
    return sorted(temp.glob("waystation-*"))


def lifecycle(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """The records of the lines a run logs per lifecycle event, in order."""
    return [r for r in caplog.records if r.name == "waystation.run"]


async def until(ready: Callable[[], bool], task: asyncio.Task[Any]) -> None:
    """Wait until ``ready()`` holds, failing at once if ``task`` ends first.

    For a run that must reach a known point — a git stalled mid-stage — before
    the test acts. It polls the file system, not a bound: the bounds under
    test are driven by ``ManualClock``.
    """
    while not ready():
        if task.done():
            pytest.fail(f"the run ended first: {task.result()!r}")
        await asyncio.sleep(0.02)


def stalling_ref_hook(hooks: Path, started: Path, release: Path) -> Path:
    """A ``reference-transaction`` hook in ``hooks``; returns the directory.

    The first ref update git prepares touches ``started``, then waits for
    ``release`` (20 s at most, so a failed test cannot hang CI): it runs inside
    ``update-ref`` while git holds the ref's lock. Point a host at it with
    ``git config core.hooksPath``.
    """
    hooks.mkdir()
    hook = hooks / "reference-transaction"
    started_at, release_at = started.as_posix(), release.as_posix()
    hook.write_bytes(
        "\n".join(
            [
                "#!/bin/sh",
                "cat > /dev/null",
                f"if [ \"$1\" = prepared ] && [ ! -f '{started_at}' ]; then",
                f"  : > '{started_at}'",
                "  i=0",
                f"  while [ ! -f '{release_at}' ] && [ $i -lt 400 ]; do",
                "    sleep 0.05; i=$((i+1))",
                "  done",
                "fi",
                "",
            ]
        ).encode()
    )
    hook.chmod(0o755)
    return hooks


def sh() -> str:
    """The POSIX sh ``ScriptedAgent`` found on this host (Git Bash on Windows)."""
    return str(ScriptedAgent().command("", {}).argv[0])


def a_run(
    repo: Path,
    prompt: str | Path = PROMPT,
    *,
    commits: Sequence[ScriptedCommit] = (ScriptedCommit("add a file", {"a.txt": "x"}),),
    sandbox: SandboxBackend | None = None,
    shell: str | None = None,
) -> RunSpec[Summary]:
    """A run that says one line, makes ``commits`` (one, by default), and reports.

    It runs on ``NoSandbox`` unless given another ``sandbox``; one that
    brings its own sh, as ``DockerSandbox`` does, wants ``shell="sh"`` too.
    """
    return Flow(
        repo,
        agent=ScriptedAgent(
            lines=["working"], outcome=OK_OUTCOME, commits=commits, shell=shell
        ),
        sandbox=sandbox if sandbox is not None else NoSandbox(),
    ).run(prompt)


async def awaited(spec: RunSpec[Any]) -> RunResult[Any]:
    """Await ``spec`` inside a coroutine, which ``asyncio.create_task`` needs."""
    result: RunResult[Any] = await spec
    return result


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
