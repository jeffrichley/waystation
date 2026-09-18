"""RunLogFiles and EventLog hook bundles (issue #34)."""

from __future__ import annotations

from pathlib import Path

import pytest

from waystation import RunSucceeded

from helpers import PROMPT, a_run


@pytest.mark.git
async def test_ctx_carries_the_prompt_from_run_start(host_repo: Path) -> None:
    seen: list[str] = []

    result = await a_run(host_repo).on_run_start(lambda ctx: seen.append(ctx.prompt))

    assert isinstance(result, RunSucceeded)
    assert seen == [PROMPT]


@pytest.mark.git
async def test_a_prompt_held_in_a_file_reaches_ctx_as_text(
    host_repo: Path, tmp_path: Path
) -> None:
    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text("Read the file.\n", encoding="utf-8")
    seen: list[str] = []

    result = await a_run(host_repo, prompt_file).on_run_start(
        lambda ctx: seen.append(ctx.prompt)
    )

    assert isinstance(result, RunSucceeded)
    assert seen == ["Read the file.\n"]
