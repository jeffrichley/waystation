"""A misconfigured run fails in seconds, before it starts (#30).

A lone awaited run preflights itself the way fan-out preflights a batch: the
agent provider, the sandbox spec, the prompt file and the host's git are
checked first, and any problem raises ``PreflightError`` naming what failed
and how to fix it. A run that fails preflight never began: no hook fires, no
workspace is made, nothing in the host repo changes, and nothing is repaired.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from helpers import OK_OUTCOME, a_run, host_state, workspaces
from waystation import (
    Errored,
    Flow,
    NoSandbox,
    PreflightError,
    RunSpec,
    RunSucceeded,
    Summary,
    preflight,
)
from waystation.agents import AgentCommand, AgentEvent
from waystation.testing import ScriptedAgent, ScriptedCommit


@dataclass
class CountingAgent:
    """A working agent that counts how many times its host check ran."""

    checks: int = 0
    inner: ScriptedAgent = field(
        default_factory=lambda: ScriptedAgent(
            commits=(ScriptedCommit("add a file", {"a.txt": "x"}),),
            outcome=OK_OUTCOME,
        )
    )

    def preflight(self) -> None:
        self.checks += 1

    def command(self, prompt: str, outcome_schema: dict[str, Any]) -> AgentCommand:
        return self.inner.command(prompt, outcome_schema)

    def parse(self, line: str) -> Sequence[AgentEvent]:
        return self.inner.parse(line)


@dataclass(frozen=True)
class AgentWithPreflight:
    """An agent provider whose host check does what the test says, and no more."""

    error: Exception | None = None

    def preflight(self) -> None:
        if self.error is not None:
            raise self.error

    def command(self, prompt: str, outcome_schema: dict[str, Any]) -> AgentCommand:
        raise AssertionError("a run that failed preflight built a command")

    def parse(self, line: str) -> Sequence[AgentEvent]:
        return ()


@dataclass(frozen=True)
class SandboxThatFailsPreflight(NoSandbox):
    """A sandbox spec that cannot be satisfied on this host."""

    error: Exception = RuntimeError("daemon unreachable")

    async def preflight(self) -> None:
        raise self.error


def _spec(repo: Path, agent: AgentWithPreflight) -> RunSpec[Summary]:
    return Flow(repo, agent=agent, sandbox=NoSandbox()).run("work")


def _counted_spec(repo: Path, agent: CountingAgent) -> RunSpec[Summary]:
    return Flow(repo, agent=agent, sandbox=NoSandbox()).run("work")


async def _refused(
    spec: RunSpec[Summary], repo: Path, temp: Path, started: list[str]
) -> PreflightError:
    """Await ``spec``, expecting preflight to stop it before anything began."""
    before = host_state(repo)
    with pytest.raises(PreflightError) as refused:
        await spec.on_run_start(lambda ctx: started.append(ctx.run_id))
    assert started == [], "a run that failed preflight fired run_start"
    assert workspaces(temp) == []
    assert host_state(repo) == before
    return refused.value


@pytest.mark.git
async def test_a_missing_prompt_file_fails_preflight_naming_it(
    host_repo: Path, tmp_path: Path, isolated_tempdir: Path
) -> None:
    missing = tmp_path / "prompts" / "fix-the-bug.md"

    error = await _refused(a_run(host_repo, missing), host_repo, isolated_tempdir, [])

    assert str(missing) in str(error)
    assert "not found" in str(error)
    assert "str" in str(error), "the fix — pass the prompt itself — is named"


@pytest.mark.git
async def test_a_prompt_path_that_is_a_directory_fails_preflight_saying_so(
    host_repo: Path, tmp_path: Path, isolated_tempdir: Path
) -> None:
    folder = tmp_path / "prompts"
    folder.mkdir()

    error = await _refused(a_run(host_repo, folder), host_repo, isolated_tempdir, [])

    assert str(folder) in str(error)
    assert "not a file" in str(error)


@pytest.mark.git
async def test_an_agent_provider_that_fails_preflight_stops_the_run_first(
    host_repo: Path, isolated_tempdir: Path
) -> None:
    refusal = PreflightError("CLAUDE_CODE_OAUTH_TOKEN is not set: export it")
    spec = _spec(host_repo, AgentWithPreflight(refusal))

    error = await _refused(spec, host_repo, isolated_tempdir, [])

    assert error is refusal


@pytest.mark.git
async def test_a_provider_bug_in_preflight_is_a_preflight_error_naming_it(
    host_repo: Path, isolated_tempdir: Path
) -> None:
    bug = KeyError("HOME")

    error = await _refused(
        _spec(host_repo, AgentWithPreflight(bug)), host_repo, isolated_tempdir, []
    )

    assert "AgentWithPreflight" in str(error)
    assert isinstance(error.failure, Errored)
    assert error.failure.exception is bug


@pytest.mark.git
async def test_a_sandbox_spec_that_fails_preflight_stops_the_run_first(
    host_repo: Path, isolated_tempdir: Path
) -> None:
    spec: RunSpec[Summary] = Flow(
        host_repo, agent=ScriptedAgent(), sandbox=SandboxThatFailsPreflight()
    ).run("work")

    error = await _refused(spec, host_repo, isolated_tempdir, [])

    assert "SandboxThatFailsPreflight" in str(error)
    assert "daemon unreachable" in str(error)


@pytest.mark.git
async def test_no_git_on_path_fails_preflight_saying_to_install_it(
    host_repo: Path,
    tmp_path: Path,
    isolated_tempdir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = host_state(host_repo)  # read with the real git, before PATH goes
    empty = tmp_path / "no-git-here"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))

    with pytest.raises(PreflightError) as refused:
        await _spec(host_repo, AgentWithPreflight())

    assert "install git" in str(refused.value)
    assert workspaces(isolated_tempdir) == []
    monkeypatch.undo()
    assert host_state(host_repo) == before


@pytest.mark.unit
async def test_the_hosts_git_is_checked_before_any_agent_or_sandbox() -> None:
    """Two true answers, and git is the one worth giving.

    A host with no git has no sh either, so `NoSandbox` would otherwise
    answer first with "install Git Bash" — and a batch would pay a docker
    daemon ping before finding out it could not run at all (ADR-0036).
    """
    order: list[str] = []

    class _Watching(NoSandbox):
        async def preflight(self) -> None:
            order.append("sandbox")

    async def _git_first() -> None:
        order.append("git")

    spec: RunSpec[Summary] = Flow(
        Path.cwd(), agent=ScriptedAgent(), sandbox=_Watching()
    ).run("work")
    with mock.patch("waystation.preflight.require_host_git", _git_first):
        await preflight([spec])

    assert order == ["git", "sandbox"]


@pytest.mark.git
@pytest.mark.skipif(
    sys.platform == "win32",
    reason="a stand-in git on PATH needs a POSIX shebang; the check is the same",
)
@pytest.mark.parametrize(
    ("stand_in", "says"),
    [
        pytest.param(
            'echo "git version 2.39.5"', ("2.39.5", "2.40", "upgrade"), id="too-old"
        ),
        pytest.param(
            'echo "hub version 2.14.2"', ("hub version 2.14.2", "PATH"), id="not-git"
        ),
        pytest.param(
            'echo "no such thing" >&2; exit 3',
            ("exited 3", "no such thing", "PATH"),
            id="fails",
        ),
    ],
)
async def test_a_host_git_that_will_not_do_fails_preflight_saying_why(
    host_repo: Path,
    tmp_path: Path,
    isolated_tempdir: Path,
    monkeypatch: pytest.MonkeyPatch,
    stand_in: str,
    says: tuple[str, ...],
) -> None:
    before = host_state(host_repo)  # read with the real git, before the stand-in
    bin_dir = tmp_path / "stand-in"
    bin_dir.mkdir()
    fake = bin_dir / "git"
    fake.write_text(f"#!/bin/sh\n{stand_in}\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")

    with pytest.raises(PreflightError) as refused:
        await _spec(host_repo, AgentWithPreflight())

    for fragment in says:
        assert fragment in str(refused.value)
    assert workspaces(isolated_tempdir) == []
    monkeypatch.undo()
    assert host_state(host_repo) == before


@pytest.mark.git
async def test_a_batch_checked_once_performs_without_checking_again(
    host_repo: Path,
) -> None:
    """Preflighting once is a property of a batch, not of fan-out (ADR-0032).

    A scheduler of the user's own checks its batch with ``preflight(specs)``
    and then performs each spec already checked — the path fan-out takes,
    rather than reaching into a private method of a spec.
    """
    counted = CountingAgent()
    specs = [_counted_spec(host_repo, counted) for _ in range(3)]

    await preflight(specs)
    results = [await spec.perform(preflighted=True) for spec in specs]

    assert all(isinstance(result, RunSucceeded) for result in results)
    assert counted.checks == 1, "one distinct provider, checked once for the batch"


@pytest.mark.git
async def test_awaiting_a_spec_checks_it_and_performing_it_is_the_same_run(
    host_repo: Path,
) -> None:
    """``await spec`` delegates to ``perform``, which is the only path there is."""
    counted = CountingAgent()
    spec = _counted_spec(host_repo, counted)

    awaited_result = await spec
    performed = await spec.perform()

    assert isinstance(awaited_result, RunSucceeded)
    assert isinstance(performed, RunSucceeded)
    assert awaited_result.run_id != performed.run_id, "each run gets a new id"
    assert counted.checks == 2, "a lone run is a batch of one, checked each time"
