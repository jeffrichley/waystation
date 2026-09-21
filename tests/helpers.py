"""Helpers every test module may reach for. Look here before writing your own.

Fixtures live in ``conftest.py``; this module holds the plain functions, so a
helper can be called from a fixture, a test body, or another helper.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import pytest
from pydantic import BaseModel

from waystation import (
    ExecResult,
    Flow,
    NoSandbox,
    RunResult,
    RunSpec,
    Sandbox,
    SandboxBackend,
    ScriptedAgent,
    ScriptedCommit,
    Summary,
    Workspace,
)
from waystation.agents import (
    AgentCommand,
    AgentEvent,
    AgentText,
    AgentUsage,
    OutcomeReported,
)
from waystation.sandbox.protocol import LineCallback

__all__ = [
    "MAKES_A_MERGE",
    "OK_OUTCOME",
    "OK_OUTCOME_LINE",
    "OUTCOME",
    "PROMPT",
    "RECORDED_CLAUDE",
    "TEST_IMAGE",
    "USAGE",
    "WORKS_UNTIL_STOPPED",
    "Gate",
    "GatedSandbox",
    "ShellAgent",
    "Total",
    "a_run",
    "awaited",
    "branches",
    "commit_on",
    "git",
    "git_bytes",
    "host_state",
    "init_host_repo",
    "lifecycle",
    "printf_bytes",
    "recorded_claude",
    "stalling_ref_hook",
    "subjects",
    "until",
    "workspaces",
]

TEST_IMAGE = "waystation-test"
"""The image the docker tier runs in; ``just test-image`` builds it."""

OK_OUTCOME = {"summary": "ok"}
"""The Outcome a scripted agent reports when the test doesn't care what it says."""

OUTCOME = "OUTCOME "
"""The marker line ``ShellAgent`` reports its Outcome on."""

OK_OUTCOME_LINE = OUTCOME + '{"summary": "ok"}'
"""A whole reporting line, for a script that only needs to finish cleanly."""

USAGE = "USAGE "
"""The marker line ``ShellAgent`` reports token usage on: ``AgentUsage`` as JSON."""

PROMPT = "Do the thing.\nWith detail on a second line."
"""A prompt with a second line, so a test can prove the body stayed unlogged."""

WORKS_UNTIL_STOPPED = "\n".join(
    [
        "set -e",
        "printf 'a\\n' > a.txt",
        "git add a.txt",
        "git commit -q -m first",
        "printf 'wip\\n' > wip.txt",
        "echo ready",
        "while true; do sleep 0.05; done",
    ]
)
"""A ``ShellAgent`` script that commits, leaves work, says ``ready``, then works.

Stopped, it leaves ``["WIP: salvaged uncommitted work", "first"]`` to keep.
It works in short sleeps, never one long child: a kill that races a spawn
on Windows can miss the child, and a missed ``sleep 60`` holds stdout open
past the test's timeout, where a missed ``sleep 0.05`` is gone at once.
"""

MAKES_A_MERGE = "\n".join(
    [
        "set -e",
        "base=$(git rev-parse HEAD)",
        "printf 'a\\n' > A",
        "git add A && git commit -q -m side-a",
        'git branch other "$base"',
        "git checkout -q other",
        "printf 'b\\n' > B",
        "git add B && git commit -q -m side-b",
        "git checkout -q -",
        "git merge -q --no-ff -m merge-commit other",
        f"echo '{OK_OUTCOME_LINE}'",
    ]
)
"""A ``ShellAgent`` script whose series is nonlinear: it ends in a merge commit.

Collect refuses such a series and squashes it to one commit (ADR-0006), so
this is what a test reaches for to drive that refusal. Append ``exit <n>``
to make the agent fail as well.
"""

RECORDED_CLAUDE = Path(__file__).parent / "fixtures" / "claude_code"
"""Real Claude Code stdout, one ``<scenario>.jsonl`` per recorded run."""


class Total(BaseModel):
    """The Outcome the ``success`` recording reports: ``numbers.txt``'s sum."""

    total: int


def recorded_claude(scenario: str) -> tuple[str, ...]:
    """The stdout of a real Claude Code run, as ``test_claude_code_live`` saved it."""
    path = RECORDED_CLAUDE / f"{scenario}.jsonl"
    return tuple(path.read_text(encoding="utf-8").splitlines())


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


def branches(repo: Path, pattern: str) -> list[str]:
    """The short names of the branches in ``repo`` matching ``pattern``."""
    listed = git(repo, "branch", "--list", "--format=%(refname:short)", pattern)
    return listed.splitlines()


def git_bytes(repo: Path, *args: str) -> bytes:
    """Run git in ``repo`` and return its stdout as git wrote it, byte for byte."""
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True
    ).stdout


def printf_bytes(path: str, data: bytes) -> str:
    """A sh command that writes ``data`` to ``path``, byte for byte.

    Every byte goes as an octal escape, so the command line is plain ASCII: a
    CR or a non-ASCII byte in a Windows command line may not reach Git Bash's
    sh intact.
    """
    escaped = "".join(f"\\{byte:03o}" for byte in data)
    return f"printf '{escaped}' > {path}"


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


def a_run(
    repo: Path,
    prompt: str | Path = PROMPT,
    *,
    commits: Sequence[ScriptedCommit] = (ScriptedCommit("add a file", {"a.txt": "x"}),),
    sandbox: SandboxBackend | None = None,
) -> RunSpec[Summary]:
    """A run that says one line, makes ``commits`` (one, by default), and reports.

    It runs on ``NoSandbox`` unless given another ``sandbox``. Whichever it
    is, the sandbox says which shell the playback runs under (ADR-0036).
    """
    return Flow(
        repo,
        agent=ScriptedAgent(lines=["working"], outcome=OK_OUTCOME, commits=commits),
        sandbox=sandbox if sandbox is not None else NoSandbox(),
    ).run(prompt)


async def awaited(spec: RunSpec[Any]) -> RunResult[Any]:
    """Await ``spec`` inside a coroutine, which ``asyncio.create_task`` needs."""
    result: RunResult[Any] = await spec
    return result


@dataclass(frozen=True)
class Gate:
    """A point a test holds a run at, cancels it there, then lets it go on.

    ``hold()`` sets ``reached``, waits for ``release``, then sets ``passed``.
    """

    reached: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    passed: asyncio.Event = field(default_factory=asyncio.Event)

    async def hold(self) -> None:
        self.reached.set()
        await self.release.wait()
        self.passed.set()


class _GatedBox:
    """A live sandbox whose git execs wait at the gate first.

    They are collect's: the agents these tests run are shell scripts.
    """

    def __init__(self, inner: Sandbox, gate: Gate) -> None:
        self.workspace = inner.workspace
        self.shell = inner.shell
        self._inner, self._gate = inner, gate

    async def exec(
        self,
        argv: Sequence[str],
        *,
        stdin: str | None = None,
        env: Mapping[str, str] | None = None,
        capture: bool = True,
        on_stdout: LineCallback | None = None,
        on_stderr: LineCallback | None = None,
    ) -> ExecResult:
        if argv[0] == "git":
            await self._gate.hold()
        return await self._inner.exec(
            argv,
            stdin=stdin,
            env=env,
            capture=capture,
            on_stdout=on_stdout,
            on_stderr=on_stderr,
        )


@dataclass(frozen=True)
class GatedSandbox:
    """``NoSandbox``, held at one point of its life until the test lets it go."""

    gate: Gate
    at: Literal["start", "collect", "teardown"]
    inner: NoSandbox = field(default_factory=NoSandbox)

    async def preflight(self) -> None:
        await self.inner.preflight()

    @asynccontextmanager
    async def start(
        self, ws: Workspace, *, env: Mapping[str, str]
    ) -> AsyncIterator[Sandbox]:
        if self.at == "start":
            await self.gate.hold()
        async with self.inner.start(ws, env=env) as box:
            yield box if self.at != "collect" else _GatedBox(box, self.gate)
            if self.at == "teardown":
                await self.gate.hold()


@dataclass(frozen=True)
class ShellAgent:
    """An agent that is a shell script; an ``OUTCOME <json>`` line reports.

    A ``USAGE <json>`` line reports token usage, as an ``AgentUsage``.

    Reach for this over ``ScriptedAgent`` when the test needs to control the
    script itself — writing to stderr, say, or exiting mid-stream. The
    sandbox says which shell it runs under, so it runs the same on every
    backend (ADR-0036).
    """

    script: str
    env: Mapping[str, str] = field(default_factory=dict)
    pass_env: Sequence[str] = ()

    def preflight(self) -> None:
        return None

    def command(self, prompt: str, outcome_schema: dict[str, Any]) -> AgentCommand:
        return AgentCommand(
            script=self.script,
            env=dict(self.env),
            pass_env=tuple(self.pass_env),
        )

    def parse(self, line: str) -> Sequence[AgentEvent]:
        if line.startswith(OUTCOME):
            return (OutcomeReported(json.loads(line.removeprefix(OUTCOME))),)
        if line.startswith(USAGE):
            return (AgentUsage(**json.loads(line.removeprefix(USAGE))),)
        return (AgentText(line),) if line else ()
