"""When a path prompt is read: once per await, before ``run_start`` (ADR-0045)."""

from __future__ import annotations

from pathlib import Path

import pytest

from helpers import RecordingAgent
from waystation import Flow, NoSandbox, RunSucceeded
from waystation.hooks import RunContext


@pytest.mark.git
async def test_each_await_of_one_run_spec_reads_the_prompt_file_afresh(
    host_repo: Path, tmp_path: Path
) -> None:
    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text("first\n", encoding="utf-8")
    agent = RecordingAgent()
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
    """What ``ctx.prompt`` shows every hook is what the agent is handed.

    The ``run_start`` hook edits the file without reading ``ctx.prompt``, so
    a read put off until something first asks for it would see the edit.
    """
    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text("as the run started\n", encoding="utf-8")
    seen: list[str] = []

    def edit(ctx: RunContext) -> None:
        prompt_file.write_text("edited at run_start\n", encoding="utf-8")

    def look_and_edit(ctx: RunContext) -> None:
        seen.append(ctx.prompt)
        prompt_file.write_text("edited later\n", encoding="utf-8")

    agent = RecordingAgent()
    result = await (
        Flow(host_repo, agent=agent, sandbox=NoSandbox())
        .run(prompt_file)
        .on_run_start(edit)
        .on_workspace_ready(look_and_edit)
        .on_sandbox_ready(look_and_edit)
    )

    assert isinstance(result, RunSucceeded)
    assert seen == ["as the run started\n"] * 2
    assert agent.prompts == ["as the run started\n"]
