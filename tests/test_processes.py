"""NoSandbox takes what is OS-specific about processes as an injected strategy."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from waystation import (
    Flow,
    NoSandbox,
    RunContext,
    RunFailed,
    RunSucceeded,
    ScriptedAgent,
)
from waystation.agents import (
    AgentCommand,
    AgentEvent,
    AgentLine,
    AgentText,
    OutcomeReported,
)
from waystation.sandbox import ProcessTree, host_processes

OUTCOME = "OUTCOME "


class Answer(BaseModel):
    summary: str


@dataclass(frozen=True)
class ShellAgent:
    """An agent that is a shell script; an ``OUTCOME <json>`` line reports."""

    script: str

    def preflight(self) -> None:
        return None

    def command(self, prompt: str, outcome_schema: dict[str, Any]) -> AgentCommand:
        sh = str(ScriptedAgent().command("", {}).argv[0])
        return AgentCommand(argv=(sh, "-c", self.script))

    def parse(self, line: str) -> Sequence[AgentEvent]:
        if line.startswith(OUTCOME):
            return (OutcomeReported(json.loads(line.removeprefix(OUTCOME))),)
        return (AgentText(line),) if line else ()


class _RecordedTree:
    def __init__(self, inner: ProcessTree, events: list[str]) -> None:
        self.inner = inner
        self.events = events

    def kill(self) -> None:
        self.events.append("kill")
        self.inner.kill()

    def release(self) -> None:
        self.events.append("release")
        self.inner.release()


class RecordingProcesses:
    """Wraps the host's strategy, recording calls and widening the env allowlist."""

    def __init__(self, base_env_keys: Sequence[str] | None = None) -> None:
        self.inner = host_processes()
        self.events: list[str] = []
        self._base_env_keys = base_env_keys

    def base_env_keys(self) -> Sequence[str]:
        if self._base_env_keys is None:
            return self.inner.base_env_keys()
        return self._base_env_keys

    def spawn_options(self) -> dict[str, Any]:
        return self.inner.spawn_options()

    def adopt(self, process: asyncio.subprocess.Process) -> ProcessTree:
        self.events.append("adopt")
        return _RecordedTree(self.inner.adopt(process), self.events)


@pytest.mark.git
@pytest.mark.asyncio
async def test_injected_strategy_kills_a_stopped_agent_and_releases_every_exec(
    host_repo: Path,
) -> None:
    processes = RecordingProcesses()
    flow = Flow(
        host_repo,
        agent=ScriptedAgent(lines=["first"], linger=True),
        sandbox=NoSandbox(processes=processes),
    )

    def stop(ctx: RunContext, line: AgentLine) -> None:
        raise RuntimeError("stop the agent")

    result = await flow.run("stop", outcome=Answer).on_agent_output(stop)

    assert isinstance(result, RunFailed)
    assert processes.events.count("kill") == 1
    assert processes.events.count("release") == processes.events.count("adopt")
    last_release = len(processes.events) - 1 - processes.events[::-1].index("release")
    assert processes.events.index("kill") < last_release


@pytest.mark.git
@pytest.mark.asyncio
async def test_the_strategy_decides_which_host_env_a_process_starts_with(
    host_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WAYSTATION_PROBE", "leaked")
    widened = RecordingProcesses(
        (*host_processes().base_env_keys(), "WAYSTATION_PROBE")
    )
    agent = ShellAgent(
        "; ".join(
            [
                'echo "probe=${WAYSTATION_PROBE:-none}"',
                f"echo '{OUTCOME}" + '{"summary": "ok"}' + "'",
            ]
        )
    )

    seen: list[str] = []
    for sandbox in (NoSandbox(), NoSandbox(processes=widened)):
        result = await (
            Flow(host_repo, agent=agent, sandbox=sandbox)
            .run("probe", outcome=Answer)
            .on_agent_output(lambda ctx, line: seen.append(line.raw))
        )
        assert isinstance(result, RunSucceeded)

    assert "probe=none" in seen
    assert "probe=leaked" in seen
