"""The ClaudeCode agent provider: its command, its parsing, its preflight (#36)."""

from __future__ import annotations

import dataclasses
import json
import logging
from collections.abc import Sequence
from typing import Any

import pytest

from waystation import ClaudeCode, PreflightError

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
