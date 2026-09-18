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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from helpers import a_run, git, workspaces
from waystation import (
    Errored,
    Flow,
    NoSandbox,
    PreflightError,
    RunSpec,
    ScriptedAgent,
    Summary,
)
from waystation.agents import AgentCommand, AgentEvent


@dataclass(frozen=True)
class AgentThatFailsPreflight:
    """An agent provider whose host check fails, the way ``ClaudeCode`` does."""

    error: Exception

    def preflight(self) -> None:
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


def _refs(repo: Path) -> str:
    return git(repo, "for-each-ref", "--format=%(refname) %(objectname)")


async def _refused(
    spec: RunSpec[Summary], repo: Path, temp: Path, started: list[str]
) -> PreflightError:
    """Await ``spec``, expecting preflight to stop it before anything began."""
    refs = _refs(repo)
    with pytest.raises(PreflightError) as refused:
        await spec.on_run_start(lambda ctx: started.append(ctx.run_id))
    assert started == [], "a run that failed preflight fired run_start"
    assert workspaces(temp) == []
    assert _refs(repo) == refs
    return refused.value


@pytest.mark.git
async def test_a_missing_prompt_file_fails_preflight_naming_it(
    host_repo: Path, tmp_path: Path, isolated_tempdir: Path
) -> None:
    missing = tmp_path / "prompts" / "fix-the-bug.md"

    error = await _refused(a_run(host_repo, missing), host_repo, isolated_tempdir, [])

    assert str(missing) in str(error)
    assert "str" in str(error), "the fix — pass the prompt itself — is named"


@pytest.mark.git
async def test_an_agent_provider_that_fails_preflight_stops_the_run_first(
    host_repo: Path, isolated_tempdir: Path
) -> None:
    refusal = PreflightError("CLAUDE_CODE_OAUTH_TOKEN is not set: export it")
    spec: RunSpec[Summary] = Flow(
        host_repo, agent=AgentThatFailsPreflight(refusal), sandbox=NoSandbox()
    ).run("work")

    error = await _refused(spec, host_repo, isolated_tempdir, [])

    assert error is refusal


@pytest.mark.git
async def test_a_provider_bug_in_preflight_is_a_preflight_error_naming_it(
    host_repo: Path, isolated_tempdir: Path
) -> None:
    bug = KeyError("HOME")
    spec: RunSpec[Summary] = Flow(
        host_repo, agent=AgentThatFailsPreflight(bug), sandbox=NoSandbox()
    ).run("work")

    error = await _refused(spec, host_repo, isolated_tempdir, [])

    assert "AgentThatFailsPreflight" in str(error)
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
@pytest.mark.skipif(
    sys.platform == "win32",
    reason="a stand-in git on PATH needs a POSIX shebang; the check is the same",
)
async def test_host_git_older_than_2_40_fails_preflight_with_an_upgrade_hint(
    host_repo: Path,
    tmp_path: Path,
    isolated_tempdir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refs = _refs(host_repo)  # read with the real git, before the stand-in
    stand_in = tmp_path / "old-git"
    stand_in.mkdir()
    fake = stand_in / "git"
    fake.write_text('#!/bin/sh\necho "git version 2.39.5"\n', encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{stand_in}:{Path('/usr/bin')}:/bin")

    with pytest.raises(PreflightError) as refused:
        await a_run(host_repo)

    assert "2.39.5" in str(refused.value)
    assert "2.40" in str(refused.value)
    assert "upgrade" in str(refused.value).lower()
    assert workspaces(isolated_tempdir) == []
    monkeypatch.undo()
    assert _refs(host_repo) == refs
