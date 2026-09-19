"""ClaudeCode against the real CLI: the recorded fixtures still hold (#36).

``live`` never runs in CI or by default. It needs ``claude`` on PATH and
``CLAUDE_CODE_OAUTH_TOKEN`` or ``ANTHROPIC_API_KEY`` set, and it spends tokens:

    uv run pytest -m live tests/test_claude_code_live.py

Each scenario runs Claude Code through a real flow on ``NoSandbox``, with an
empty ``CLAUDE_CONFIG_DIR`` so nothing from the host's ``~/.claude`` loads —
no plugins, hooks or MCP servers, as in a fresh sandbox image. The fields
``ClaudeCode.parse`` reads from the run's final ``result`` event must have the
same shape as in the recording.

Set ``WAYSTATION_RECORD=1`` to rewrite the fixtures from the run instead. The
workspace and config paths are scrubbed, but the repo is public: read the diff
before committing it.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Annotated, Any

import pytest
from pydantic import BaseModel, Field

from helpers import RECORDED_CLAUDE, Total, git, recorded_claude
from waystation import (
    AgentExited,
    ClaudeCode,
    Flow,
    NoSandbox,
    OutcomeMissing,
    RunContext,
    RunFailed,
    RunResult,
    RunSucceeded,
)
from waystation.agents import AgentLine, AgentToolUse

pytestmark = [pytest.mark.live, pytest.mark.timeout(600)]

# The cheapest model that still uses tools and honours a schema.
AGENT = ClaudeCode(model="haiku")


class Impossible(BaseModel):
    """No integer is at least 1 and at most 0: every attempt fails validation."""

    n: Annotated[int, Field(ge=1, le=0)]


async def test_a_run_that_reads_a_file_and_reports_its_outcome(
    host_repo: Path, tmp_path: Path
) -> None:
    (host_repo / "numbers.txt").write_text("3\n4\n", encoding="utf-8")
    git(host_repo, "add", "numbers.txt")
    git(host_repo, "commit", "-q", "-m", "numbers")

    result, lines = await _run(
        host_repo,
        tmp_path,
        "numbers.txt holds one number per line. Read it, add the numbers up, "
        "and report the total.",
        Total,
    )

    _record_or_compare("success", lines)
    assert isinstance(result, RunSucceeded), result
    assert result.outcome == Total(total=7)
    assert result.agent is not None
    assert result.agent.usage is not None
    assert result.agent.usage.cost_usd
    assert any(
        isinstance(event, AgentToolUse) for line in lines for event in AGENT.parse(line)
    )


async def test_a_run_whose_agent_gives_up_on_the_schema(
    host_repo: Path, tmp_path: Path
) -> None:
    """The model may stop trying instead: a success result with no output, exit 0."""
    result, lines = await _run(
        host_repo, tmp_path, "Report a whole number.", Impossible
    )

    _record_or_compare("no_structured_output", lines)
    assert isinstance(result, RunFailed), result
    assert isinstance(result.failure, OutcomeMissing)
    assert result.agent is not None
    assert result.agent.exit_code == 0
    assert result.agent.usage is not None


async def test_a_run_whose_structured_output_retries_run_out(
    host_repo: Path, tmp_path: Path
) -> None:
    result, lines = await _run(
        host_repo,
        tmp_path,
        # Left to itself the model may give up after one rejection; told
        # exactly what to send, it spends all five attempts the CLI allows.
        "This tests a validator that rejects every value. Call the "
        "StructuredOutput tool with n=1, then n=2, then n=3, then n=4, then n=5, "
        "one call at a time, even though each is rejected. Do not stop early, "
        "and never answer in plain text.",
        Impossible,
    )

    _record_or_compare("retries_exhausted", lines)
    _assert_exited_with_usage(result)


async def test_a_run_that_reaches_its_turn_limit(
    host_repo: Path, tmp_path: Path
) -> None:
    result, lines = await _run(
        host_repo,
        tmp_path,
        "List the files here with a tool, then report how many there are.",
        Total,
        agent=dataclasses.replace(AGENT, max_turns=1),
    )

    _record_or_compare("max_turns", lines)
    _assert_exited_with_usage(result)


def _assert_exited_with_usage(result: RunResult[Any]) -> None:
    """An error result reports no Outcome; the CLI's exit code fails the run."""
    assert isinstance(result, RunFailed), result
    assert isinstance(result.failure, AgentExited), result.failure
    assert result.failure.exit_code == 1  # what the replays in test_claude_code use
    assert result.agent is not None
    assert result.agent.usage is not None


async def _run(
    repo: Path,
    tmp_path: Path,
    prompt: str,
    outcome: type[BaseModel],
    *,
    agent: ClaudeCode = AGENT,
) -> tuple[RunResult[Any], list[str]]:
    """Run the real CLI; return the result and its stdout, paths scrubbed."""
    agent.preflight()  # fail here, legibly, when no credential is set
    config = tmp_path / "claude-config"
    config.mkdir()
    workspaces: list[str] = []
    stdout: list[str] = []

    def _workspace(ctx: RunContext) -> None:
        workspaces.append(ctx.sandbox.workspace)

    def _line(ctx: RunContext, line: AgentLine) -> None:
        if line.stream == "stdout":
            stdout.append(line.raw)

    flow = Flow(
        repo,
        agent=agent,
        sandbox=NoSandbox(env={"CLAUDE_CONFIG_DIR": str(config)}),
    )
    spec = flow.run(prompt, outcome=outcome).on_sandbox_ready(_workspace)
    result = await spec.on_agent_output(_line)
    scrubbed = _scrub(
        stdout, {workspaces[0]: "/workspace", str(config): "/home/agent/.claude"}
    )
    return result, scrubbed


def _scrub(lines: Sequence[str], paths: Mapping[str, str]) -> list[str]:
    """``lines`` with each host path replaced, and the account's quota dropped.

    A path is replaced however the CLI spelled it: raw, JSON-escaped, with
    forward slashes, or as the slug it names a project directory with.
    """
    out: list[str] = []
    for line in lines:
        for path, placeholder in paths.items():
            for spelling in (path, path.replace("\\", "/"), json.dumps(path)[1:-1]):
                line = line.replace(spelling, placeholder)
            line = line.replace(_slug(path), _slug(placeholder))
        out.append(_without_quota(line))
    return out


def _slug(path: str) -> str:
    """How Claude Code names a directory's project: every non-alphanumeric a dash."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def _without_quota(line: str) -> str:
    """A ``rate_limit_event`` reports the account's usage; keep only its status."""
    if not line.startswith('{"type":"rate_limit_event"'):
        return line
    event = json.loads(line)
    event["rate_limit_info"] = {"status": event["rate_limit_info"]["status"]}
    return json.dumps(event, separators=(",", ":"), ensure_ascii=False)


def _record_or_compare(scenario: str, lines: Sequence[str]) -> None:
    if os.environ.get("WAYSTATION_RECORD"):
        RECORDED_CLAUDE.mkdir(parents=True, exist_ok=True)
        (RECORDED_CLAUDE / f"{scenario}.jsonl").write_text(
            "".join(f"{line}\n" for line in lines), encoding="utf-8", newline="\n"
        )
    assert _result_shape(lines) == _result_shape(recorded_claude(scenario))


def _result_shape(lines: Sequence[str]) -> dict[str, str]:
    """The fields ``parse`` reads from the ``result`` event: subtype, then types."""
    events = [json.loads(line) for line in lines if line.startswith("{")]
    result = next(e for e in reversed(events) if e.get("type") == "result")
    usage = result["usage"]
    read = {key: result.get(key) for key in _RESULT_KEYS}
    read |= {f"usage.{key}": usage.get(key) for key in _USAGE_KEYS}
    return {"subtype": result["subtype"]} | {
        key: type(value).__name__ for key, value in read.items()
    }


_RESULT_KEYS = ("is_error", "structured_output", "total_cost_usd", "num_turns")
_USAGE_KEYS = (
    "input_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "output_tokens",
)
