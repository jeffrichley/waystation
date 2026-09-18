"""The ClaudeCode agent provider: its command, its parsing, its preflight (#36)."""

from __future__ import annotations

import dataclasses
import json
import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from helpers import RECORDED_CLAUDE, ShellAgent, Total, recorded_claude
from waystation import (
    AgentExited,
    ClaudeCode,
    Flow,
    NoSandbox,
    OutcomeMissing,
    PreflightError,
    RunFailed,
    RunSpec,
    RunSucceeded,
)
from waystation.agents import (
    AgentCommand,
    AgentEvent,
    AgentText,
    AgentToolUse,
    AgentUsage,
    OutcomeReported,
)

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
}


def _flag(argv: Sequence[str], name: str) -> str | None:
    """The value that follows ``name`` in ``argv``, or ``None`` if it is absent."""
    if name not in argv:
        return None
    return argv[list(argv).index(name) + 1]


@pytest.mark.unit
def test_claude_code_streams_json_in_print_mode_with_the_prompt_on_stdin() -> None:
    command = ClaudeCode().command("Fix the bug.\nThen report.", SCHEMA)

    assert command.argv[0] == "claude"
    assert {"-p", "--verbose"} <= set(command.argv)
    assert _flag(command.argv, "--output-format") == "stream-json"
    assert command.stdin == "Fix the bug.\nThen report."
    assert not any("Fix the bug" in arg for arg in command.argv)


@pytest.mark.unit
def test_claude_code_hands_the_outcome_schema_to_the_cli() -> None:
    command = ClaudeCode().command("p", SCHEMA)

    schema = _flag(command.argv, "--json-schema")
    assert schema is not None
    assert json.loads(schema) == SCHEMA


@pytest.mark.unit
@pytest.mark.parametrize(
    ("setting", "flag", "value"),
    [
        ({"model": "haiku"}, "--model", "haiku"),
        ({"permission_mode": "acceptEdits"}, "--permission-mode", "acceptEdits"),
        ({"max_turns": 12}, "--max-turns", "12"),
        ({"max_budget_usd": 2.5}, "--max-budget-usd", "2.5"),
    ],
)
def test_claude_code_passes_each_typed_setting_as_its_cli_flag(
    setting: dict[str, Any], flag: str, value: str
) -> None:
    command = ClaudeCode(**setting).command("p", SCHEMA)

    assert _flag(command.argv, flag) == value


@pytest.mark.unit
def test_an_unattended_claude_bypasses_permissions_and_is_otherwise_unbounded() -> None:
    """Nothing is bounded by default (ADR-0017); nobody is there to approve a tool."""
    argv = ClaudeCode().command("p", SCHEMA).argv

    assert _flag(argv, "--permission-mode") == "bypassPermissions"
    assert {"--model", "--max-turns", "--max-budget-usd"}.isdisjoint(argv)


@pytest.mark.unit
def test_extra_args_come_after_everything_waystation_passes() -> None:
    argv = ClaudeCode(model="haiku", args=("--model", "opus")).command("p", SCHEMA).argv

    assert tuple(argv[-2:]) == ("--model", "opus")


@pytest.mark.unit
def test_claude_code_passes_both_credentials_through_beside_the_users_own() -> None:
    command = ClaudeCode(env={"CI": "1"}, pass_env=("GH_TOKEN",)).command("p", SCHEMA)

    assert command.env == {"CI": "1"}
    assert set(command.pass_env) == {
        "CLAUDE_CODE_OAUTH_TOKEN",
        "ANTHROPIC_API_KEY",
        "GH_TOKEN",
    }


@pytest.mark.unit
def test_one_claude_code_can_serve_every_run_of_a_fan_out() -> None:
    """Frozen, so no run can change the provider another run is using."""
    provider = ClaudeCode(model="haiku")

    with pytest.raises(dataclasses.FrozenInstanceError):
        provider.model = "opus"  # type: ignore[misc]


OAUTH = "CLAUDE_CODE_OAUTH_TOKEN"
API_KEY = "ANTHROPIC_API_KEY"
SECRET = "sk-ant-oat01-not-a-real-token"


@pytest.fixture
def no_credentials(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """A host with neither credential set; set one back with ``setenv``."""
    monkeypatch.delenv(OAUTH, raising=False)
    monkeypatch.delenv(API_KEY, raising=False)
    return monkeypatch


def _agent_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "waystation.agent"]


@pytest.mark.unit
def test_preflight_fails_when_the_host_has_no_credential(
    no_credentials: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(PreflightError, match=f"{OAUTH}.*{API_KEY}"):
        ClaudeCode().preflight()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("host", "used"),
    [
        ({OAUTH: SECRET}, OAUTH),
        ({API_KEY: SECRET}, API_KEY),
        # The CLI's own precedence: in print mode an API key beats a token.
        ({OAUTH: SECRET, API_KEY: SECRET}, API_KEY),
    ],
)
def test_preflight_says_which_credential_claude_will_use_never_its_value(
    no_credentials: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    host: dict[str, str],
    used: str,
) -> None:
    for name, value in host.items():
        no_credentials.setenv(name, value)

    with caplog.at_level(logging.INFO, logger="waystation"):
        ClaudeCode().preflight()

    records = _agent_records(caplog)
    assert [r.levelno for r in records] == [logging.INFO]
    assert used in records[0].getMessage()
    assert all(SECRET not in r.getMessage() for r in caplog.records)


@pytest.mark.unit
def test_a_credential_given_to_the_provider_itself_satisfies_preflight(
    no_credentials: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger="waystation"):
        ClaudeCode(env={OAUTH: SECRET}).preflight()

    assert OAUTH in _agent_records(caplog)[0].getMessage()


@pytest.mark.unit
def test_claude_code_never_runs_bare() -> None:
    """``--bare`` skips the plugins, skills and settings a sandboxed agent needs."""
    command = ClaudeCode(args=("--add-dir", "/data")).command("p", SCHEMA)

    assert "--bare" not in command.argv


# parse, on stdout recorded from real Claude Code runs (tests/fixtures/claude_code).
# Expected values are read off each recording's final result event by hand.


def _events(scenario: str) -> list[AgentEvent]:
    """Every event ``parse`` yields over a recorded run, in order."""
    provider = ClaudeCode()
    return [e for line in recorded_claude(scenario) for e in provider.parse(line)]


def _of[E](kind: type[E], events: Sequence[AgentEvent]) -> list[E]:
    return [event for event in events if isinstance(event, kind)]


def _first(scenario: str, marker: str) -> str:
    """The first recorded line of ``scenario`` that mentions ``marker``."""
    return next(line for line in recorded_claude(scenario) if marker in line)


@pytest.mark.unit
def test_a_run_reports_its_structured_output_as_the_outcome() -> None:
    assert _of(OutcomeReported, _events("success")) == [OutcomeReported({"total": 7})]


@pytest.mark.unit
@pytest.mark.parametrize(
    "scenario",
    [
        "retries_exhausted",  # error_max_structured_output_retries
        "max_turns",  # error_max_turns
        "no_structured_output",  # a success result that carries none
    ],
)
def test_a_result_without_structured_output_reports_no_outcome(scenario: str) -> None:
    assert _of(OutcomeReported, _events(scenario)) == []


@pytest.mark.unit
@pytest.mark.parametrize(
    ("scenario", "usage"),
    [
        (
            "success",
            AgentUsage(
                input_tokens=18 + 25687 + 25918,
                output_tokens=295,
                cache_read_tokens=25687,
                cache_write_tokens=25918,
                cost_usd=0.0558977,
                turns=3,
            ),
        ),
        (
            "retries_exhausted",
            AgentUsage(
                input_tokens=34 + 96116 + 8946,
                output_tokens=703,
                cache_read_tokens=96116,
                cache_write_tokens=8946,
                cost_usd=0.031052600000000003,
                turns=6,
            ),
        ),
    ],
)
def test_a_run_reports_its_usage_with_cache_tokens_counted_as_input(
    scenario: str, usage: AgentUsage
) -> None:
    """OpenTelemetry's input tokens include the cache; Anthropic's exclude it."""
    assert _of(AgentUsage, _events(scenario)) == [usage]


@pytest.mark.unit
def test_each_tool_call_is_reported_by_name_with_its_input() -> None:
    assert _of(AgentToolUse, _events("success")) == [
        AgentToolUse("Read", {"file_path": "/workspace\\numbers.txt"}),
        AgentToolUse("StructuredOutput", {"total": 7}),
    ]


@pytest.mark.unit
def test_the_agents_text_is_reported_and_its_thinking_is_not() -> None:
    assert _of(AgentText, _events("max_turns")) == [
        AgentText("I'll list the files in the current directory for you.")
    ]
    assert _of(AgentText, _events("success")) == []  # it only thought


@pytest.mark.unit
@pytest.mark.parametrize(
    "line",
    [
        _first("success", '"subtype":"init"'),
        _first("success", '"type":"rate_limit_event"'),
        _first("success", '"subtype":"thinking_tokens"'),
        _first("success", '"type":"tool_result"'),
        "",
        "not json at all",
        '{"type": "result", "subtype": "succ',
        "[1, 2, 3]",
        '"a string"',
        '{"type": "assistant", "message": null}',
        '{"type": "assistant", "message": {"content": "not a list"}}',
        '{"type": "assistant", "message": {"content": [7, {"type": "tool_use"}]}}',
        '{"type": "result", "subtype": "success", "usage": "none"}',
    ],
)
def test_a_line_it_does_not_recognise_yields_no_events(line: str) -> None:
    """A line parse can't read must never fail the run: an unknown shape is noise."""
    assert list(ClaudeCode().parse(line)) == []


# A recorded run replayed through a real flow: sh plays the stdout back and
# ClaudeCode parses it, so the run's result is what the real CLI would give.


@dataclasses.dataclass(frozen=True)
class Replayed:
    """A recorded Claude Code run as an agent: its stdout, then its exit code."""

    scenario: str
    exit_code: int = 0

    def preflight(self) -> None:
        return None

    def command(self, prompt: str, outcome_schema: dict[str, Any]) -> AgentCommand:
        recording = (RECORDED_CLAUDE / f"{self.scenario}.jsonl").as_posix()
        script = f"cat '{recording}'\nexit {self.exit_code}"
        return ShellAgent(script).command(prompt, outcome_schema)

    def parse(self, line: str) -> Sequence[AgentEvent]:
        return ClaudeCode().parse(line)


def _replay(repo: Path, agent: Replayed) -> RunSpec[Total]:
    return Flow(repo, agent=agent, sandbox=NoSandbox()).run("p", outcome=Total)


@pytest.mark.git
async def test_a_claude_code_run_succeeds_with_its_outcome_and_usage(
    host_repo: Path,
) -> None:
    result = await _replay(host_repo, Replayed("success"))

    assert isinstance(result, RunSucceeded), result
    assert result.outcome == Total(total=7)
    assert result.agent is not None
    assert result.agent.usage is not None
    assert result.agent.usage.input_tokens == 18 + 25687 + 25918
    assert result.agent.usage.cost_usd == 0.0558977


@pytest.mark.git
async def test_an_error_result_fails_the_run_through_the_exit_code(
    host_repo: Path,
) -> None:
    """Claude exits 1 once its structured-output retries run out."""
    result = await _replay(host_repo, Replayed("retries_exhausted", exit_code=1))

    assert isinstance(result, RunFailed), result
    assert isinstance(result.failure, AgentExited)
    assert result.failure.exit_code == 1
    assert result.failure.outcome is None
    assert "error_max_structured_output_retries" in result.failure.stdout_tail
    assert result.agent is not None
    assert result.agent.usage is not None
    assert result.agent.usage.cost_usd == 0.031052600000000003


@pytest.mark.git
async def test_an_agent_that_gives_up_on_the_schema_reports_no_outcome(
    host_repo: Path,
) -> None:
    """A success result without structured output, and exit 0: nothing reported."""
    result = await _replay(host_repo, Replayed("no_structured_output"))

    assert isinstance(result, RunFailed), result
    assert isinstance(result.failure, OutcomeMissing)
