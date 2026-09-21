"""Public run API: typed failures (issue #21)."""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from helpers import ShellAgent
from waystation import (
    AgentExited,
    Errored,
    Flow,
    NoSandbox,
    OutcomeInvalid,
    OutcomeMissing,
    RunFailed,
    ScriptedAgent,
    StageError,
    WaystationError,
    prepare_workspace,
    run_agent,
)
from waystation.agents.protocol import AgentCommand, AgentEvent


class Answer(BaseModel):
    summary: str


@pytest.mark.git
@pytest.mark.asyncio
async def test_exit_0_no_outcome_returns_outcome_missing(host_repo: Path) -> None:
    flow = Flow(
        host_repo,
        agent=ScriptedAgent(lines=["hello"], outcome=None),
        sandbox=NoSandbox(),
    )
    result = await flow.run("missing", outcome=Answer)

    assert isinstance(result, RunFailed)
    assert result.stage == "agent"
    assert isinstance(result.failure, OutcomeMissing)
    assert "hello" in result.failure.stdout_tail
    assert result.agent is not None
    assert result.agent.exit_code == 0


@pytest.mark.git
@pytest.mark.asyncio
async def test_nonzero_exit_returns_agent_exited_with_outcome(
    host_repo: Path,
) -> None:
    flow = Flow(
        host_repo,
        agent=ScriptedAgent(outcome=Answer(summary="kept"), exit_code=7),
        sandbox=NoSandbox(),
    )
    result = await flow.run("crash", outcome=Answer)

    assert isinstance(result, RunFailed)
    assert result.stage == "agent"
    assert isinstance(result.failure, AgentExited)
    assert result.failure.exit_code == 7
    assert result.failure.outcome == Answer(summary="kept")
    assert result.agent is not None
    assert result.agent.exit_code == 7


@pytest.mark.git
@pytest.mark.parametrize("code", [126, 137])
async def test_an_agent_that_exits_126_or_137_is_never_run_again(
    host_repo: Path, tmp_path: Path, code: int
) -> None:
    # Only waystation's own setup is retried; an agent's run is prompt
    # content, and retrying it is the flow's call (ADR-0016).
    tries = tmp_path / "tries"
    agent = ShellAgent(f"echo try >> '{tries.as_posix()}'; exit {code}")
    flow = Flow(host_repo, agent=agent, sandbox=NoSandbox())

    result = await flow.run("once", outcome=Answer)

    assert isinstance(result, RunFailed)
    assert result.stage == "agent"
    assert isinstance(result.failure, AgentExited)
    assert result.failure.exit_code == code
    assert tries.read_text(encoding="utf-8").splitlines() == ["try"]


@pytest.mark.git
@pytest.mark.asyncio
async def test_invalid_outcome_returns_outcome_invalid(host_repo: Path) -> None:
    flow = Flow(
        host_repo,
        agent=ScriptedAgent(outcome={"nope": True}),
        sandbox=NoSandbox(),
    )
    result = await flow.run("bad", outcome=Answer)

    assert isinstance(result, RunFailed)
    assert isinstance(result.failure, OutcomeInvalid)
    assert result.failure.raw == {"nope": True}
    assert isinstance(result.failure.error, ValidationError)


@pytest.mark.git
@pytest.mark.asyncio
async def test_last_valid_outcome_wins_over_later_invalid(
    host_repo: Path,
) -> None:
    from waystation.agents.outcome import OUTCOME_MARKER

    agent = ScriptedAgent(
        lines=[
            f'{OUTCOME_MARKER} {{"summary": "first"}}',
            f'{OUTCOME_MARKER} {{"bad": true}}',
        ],
        outcome=None,
    )
    flow = Flow(host_repo, agent=agent, sandbox=NoSandbox())
    result = await flow.run("last-valid", outcome=Answer)
    assert not isinstance(result, RunFailed)
    assert result.outcome == Answer(summary="first")


@pytest.mark.git
@pytest.mark.asyncio
async def test_provider_command_exception_returns_errored(
    host_repo: Path,
) -> None:
    class BoomAgent:
        def preflight(self) -> None:
            return None

        def command(self, prompt: str, outcome_schema: dict[str, Any]) -> AgentCommand:
            raise RuntimeError("command blew up")

        def parse(self, line: str) -> list[AgentEvent]:
            return []

    flow = Flow(host_repo, agent=BoomAgent(), sandbox=NoSandbox())
    result = await flow.run("boom", outcome=Answer)

    assert isinstance(result, RunFailed)
    assert result.stage == "agent"
    assert isinstance(result.failure, Errored)
    assert isinstance(result.failure.exception, RuntimeError)
    assert "command blew up" in str(result.failure.exception)


@pytest.mark.git
@pytest.mark.asyncio
async def test_base_exception_propagates_from_awaited_run(
    host_repo: Path,
) -> None:
    class CancelAgent:
        def preflight(self) -> None:
            return None

        def command(self, prompt: str, outcome_schema: dict[str, Any]) -> AgentCommand:
            raise KeyboardInterrupt

        def parse(self, line: str) -> list[AgentEvent]:
            return []

    flow = Flow(host_repo, agent=CancelAgent(), sandbox=NoSandbox())
    with pytest.raises(KeyboardInterrupt):
        await flow.run("cancel", outcome=Answer)


@pytest.mark.git
@pytest.mark.asyncio
async def test_run_agent_primitive_raises_stage_error(host_repo: Path) -> None:
    ws = await prepare_workspace(host_repo)
    try:
        backend = NoSandbox()
        agent = ScriptedAgent(outcome=None)
        async with backend.start(ws, env={}) as sandbox:
            cmd = agent.command("p", {"type": "object"})
            with pytest.raises(StageError) as caught:
                await run_agent(sandbox, agent, cmd, Answer)
            assert isinstance(caught.value, WaystationError)
            assert caught.value.stage == "agent"
            assert isinstance(caught.value.failure, OutcomeMissing)
    finally:
        import shutil

        shutil.rmtree(ws.path, ignore_errors=True)


@pytest.mark.git
@pytest.mark.asyncio
async def test_raw_invalid_outcome_string_returns_outcome_invalid(
    host_repo: Path,
) -> None:
    flow = Flow(
        host_repo,
        agent=ScriptedAgent(outcome="not-json"),
        sandbox=NoSandbox(),
    )
    result = await flow.run("raw-bad", outcome=Answer)
    assert isinstance(result, RunFailed)
    assert isinstance(result.failure, OutcomeInvalid)
    assert result.failure.raw == "not-json"


@pytest.mark.git
@pytest.mark.asyncio
async def test_bad_base_ref_returns_command_failed(host_repo: Path) -> None:
    from waystation import CommandFailed

    flow = Flow(
        host_repo,
        agent=ScriptedAgent(outcome=Answer(summary="x")),
        sandbox=NoSandbox(),
        base="refs/heads/does-not-exist",
    )
    result = await flow.run("bad-base", outcome=Answer)
    assert isinstance(result, RunFailed)
    assert result.stage == "workspace"
    assert isinstance(result.failure, CommandFailed)
    assert result.failure.exit_code != 0


@pytest.mark.git
@pytest.mark.asyncio
async def test_provider_parse_exception_returns_errored(host_repo: Path) -> None:
    class ParseBoom(ScriptedAgent):
        def parse(self, line: str) -> list[AgentEvent]:
            raise RuntimeError("parse blew up")

    flow = Flow(
        host_repo,
        agent=ParseBoom(outcome=Answer(summary="x")),
        sandbox=NoSandbox(),
    )
    result = await flow.run("parse-boom", outcome=Answer)
    assert isinstance(result, RunFailed)
    assert result.stage == "agent"
    assert isinstance(result.failure, Errored)
    assert "parse blew up" in str(result.failure.exception)


@pytest.mark.git
@pytest.mark.asyncio
async def test_no_git_identity_returns_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from waystation import Refused

    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    repo = tmp_path / "bare"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    (repo / "f").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "f"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=T", "-c", "user.email=t@e.com", "commit", "-m", "i"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    flow = Flow(
        repo,
        agent=ScriptedAgent(outcome=Answer(summary="x")),
        sandbox=NoSandbox(),
    )
    result = await flow.run("no-id", outcome=Answer)
    assert isinstance(result, RunFailed)
    assert result.stage == "workspace"
    assert isinstance(result.failure, Refused)
    assert result.failure.reason == "no_git_identity"


@pytest.mark.git
@pytest.mark.asyncio
async def test_a_workspace_that_will_not_go_is_logged_and_the_run_still_succeeds(
    host_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import shutil

    def boom(path: object, *args: object, **kwargs: object) -> None:
        raise OSError("busy")

    monkeypatch.setattr(shutil, "rmtree", boom)
    flow = Flow(
        host_repo,
        agent=ScriptedAgent(outcome=Answer(summary="ok")),
        sandbox=NoSandbox(),
    )
    with caplog.at_level(logging.ERROR, logger="waystation"):
        result = await flow.run("teardown", outcome=Answer)
    assert not isinstance(result, RunFailed)
    assert result.outcome == Answer(summary="ok")
    assert any("could not remove the workspace" in r.message for r in caplog.records)


@pytest.mark.git
async def test_an_agent_the_sandbox_does_not_have_says_the_sandbox_is_missing_it(
    host_repo: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Exit 127 is the one code that blames the sandbox rather than the agent.

    Preflight cannot catch it — what an image holds is only knowable inside
    it — so the least it can do is say so plainly (#77, ADR-0036).
    """
    agent = ShellAgent("waystation-no-such-binary")

    with caplog.at_level(logging.ERROR, logger="waystation"):
        result = await Flow(host_repo, agent=agent, sandbox=NoSandbox()).run("go")

    assert isinstance(result, RunFailed)
    assert isinstance(result.failure, AgentExited)
    assert result.failure.exit_code == 127
    said = [r.getMessage() for r in caplog.records if r.name == "waystation.agent"]
    # What 127 usually means, and what the shell actually said — not a
    # verdict, because an agent may exit 127 meaning something else.
    assert any("exited 127" in m for m in said), said
    assert any("could not find" in m for m in said), said
    assert any("waystation-no-such-binary" in m for m in said), said
