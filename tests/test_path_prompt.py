"""When a path prompt is read: once per await, before ``run_start`` (ADR-0045)."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from helpers import OK_OUTCOME
from waystation import Flow, NoSandbox, RunSucceeded
from waystation.agents import AgentCommand, AgentEvent
from waystation.hooks import RunContext
from waystation.testing import ScriptedAgent


@dataclass
class _Recording:
    """A provider that remembers each prompt it was handed, then plays back."""

    prompts: list[str] = field(default_factory=list)
    inner: ScriptedAgent = field(
        default_factory=lambda: ScriptedAgent(outcome=OK_OUTCOME)
    )

    def preflight(self) -> None:
        self.inner.preflight()

    def command(self, prompt: str, outcome_schema: dict[str, Any]) -> AgentCommand:
        self.prompts.append(prompt)
        return self.inner.command(prompt, outcome_schema)

    def parse(self, line: str) -> Sequence[AgentEvent]:
        return self.inner.parse(line)


@pytest.mark.git
async def test_each_await_of_one_spec_reads_the_prompt_file_afresh(
    host_repo: Path, tmp_path: Path
) -> None:
    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text("first\n", encoding="utf-8")
    agent = _Recording()
    spec = Flow(host_repo, agent=agent, sandbox=NoSandbox()).run(prompt_file)

    first = await spec
    prompt_file.write_text("second\n", encoding="utf-8")
    second = await spec

    assert isinstance(first, RunSucceeded)
    assert isinstance(second, RunSucceeded)
    assert agent.prompts == ["first\n", "second\n"]


@pytest.mark.git
async def test_the_prompt_file_is_read_before_run_start_and_not_again(
    host_repo: Path, tmp_path: Path
) -> None:
    """What ``ctx.prompt`` shows every hook is what the agent is handed."""
    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text("as the run started\n", encoding="utf-8")
    seen: list[str] = []

    def edit_at(point: str) -> Callable[[RunContext], None]:
        def hook(ctx: RunContext) -> None:
            seen.append(ctx.prompt)
            prompt_file.write_text(f"edited at {point}\n", encoding="utf-8")

        return hook

    agent = _Recording()
    result = await (
        Flow(host_repo, agent=agent, sandbox=NoSandbox())
        .run(prompt_file)
        .on_run_start(edit_at("run_start"))
        .on_workspace_ready(edit_at("workspace_ready"))
        .on_sandbox_ready(edit_at("sandbox_ready"))
    )

    assert isinstance(result, RunSucceeded)
    assert seen == ["as the run started\n"] * 3
    assert agent.prompts == ["as the run started\n"]
