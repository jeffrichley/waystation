"""Logging core: configure_logging, levels, redaction, ctx.log (issue #33)."""

from __future__ import annotations

import io
import logging
import sys
from pathlib import Path

import pytest
from rich.console import Console
from rich.logging import RichHandler

from helpers import PROMPT, a_run
from waystation import (
    CommandFailed,
    RunSucceeded,
    configure_logging,
)
from waystation.agents.outcome import OUTCOME_MARKER
from waystation.hooks import HOOK_NAMES, HookRegistry, RunContext
from waystation.observability import redact_argv
from waystation.observers import RunLog


def test_redact_argv_elides_environment_values_by_key() -> None:
    argv = ("docker", "run", "-e", "ANTHROPIC_API_KEY=sk-ant-secret", "image")
    assert redact_argv(argv) == (
        "docker",
        "run",
        "-e",
        "ANTHROPIC_API_KEY=***",
        "image",
    )


def test_redact_argv_sweeps_bare_token_patterns() -> None:
    argv = (
        "claude",
        "--api-key",
        "sk-ant-api03-Zm9vYmFyYmF6cXV1eA",
        "--gh",
        "ghp_0123456789abcdefghijABCDEFGHIJ0123",
        "--aws",
        "AKIAIOSFODNN7EXAMPLE",
    )
    assert redact_argv(argv) == (
        "claude",
        "--api-key",
        "***",
        "--gh",
        "***",
        "--aws",
        "***",
    )


def test_redact_argv_elides_credentials_embedded_in_a_remote_url() -> None:
    argv = ("git", "push", "https://x-access-token:ghp_secret@github.com/a/b.git")
    assert redact_argv(argv) == (
        "git",
        "push",
        "https://x-access-token:***@github.com/a/b.git",
    )


def test_redact_argv_keeps_ordinary_git_arguments() -> None:
    argv = (
        "git",
        "-C",
        "/repo",
        "-c",
        "user.email=dev@example.test",
        "commit",
        "-m",
        "fix: a thing",
    )
    assert redact_argv(argv) == argv


def test_configure_logging_installs_exactly_one_stderr_handler(
    clean_logging: None,
) -> None:
    console = configure_logging()
    again = configure_logging("DEBUG")

    logger = logging.getLogger("waystation")
    installed = [h for h in logger.handlers if isinstance(h, RichHandler)]
    assert len(installed) == 1
    assert installed[0].console is console is again
    assert console.file is sys.stderr
    assert logger.level == logging.DEBUG


def test_configure_logging_writes_the_run_tag_to_stderr_only(
    clean_logging: None, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging()
    logging.getLogger("waystation.run").info("hello", extra={"run_id": "0badcafe"})

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "hello" in captured.err
    assert "0badcafe" in captured.err


def test_importing_waystation_installs_no_handler_of_its_own() -> None:
    logger = logging.getLogger("waystation")
    assert [type(h) for h in logger.handlers] == [logging.NullHandler]


@pytest.mark.git
async def test_a_run_prints_nothing_until_a_console_is_asked_for(
    host_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    result = await a_run(host_repo)

    assert isinstance(result, RunSucceeded)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def run_id_of(record: logging.LogRecord) -> str | None:
    """The run tag a record carries; ``extra`` is invisible to a type checker."""
    return getattr(record, "run_id", None)


def run_name_of(record: logging.LogRecord) -> str | None:
    return getattr(record, "run_name", None)


def lifecycle(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "waystation.run"]


@pytest.mark.git
async def test_a_run_logs_one_tagged_line_per_lifecycle_event(
    host_repo: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="waystation"):
        result = await a_run(host_repo).integrate("landing")

    assert isinstance(result, RunSucceeded)
    records = lifecycle(caplog)
    assert [r.getMessage().split(":")[0] for r in records] == [
        "run start",
        "workspace ready",
        "sandbox up",
        "agent start",
        "agent end",
        "integrated",
        "run end",
    ]
    assert {run_id_of(r) for r in records} == {result.run_id}
    assert {run_name_of(r) for r in records} == {None}


@pytest.mark.git
async def test_the_prompt_body_never_reaches_a_log_record(
    host_repo: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG, logger="waystation"):
        await a_run(host_repo)

    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert f"prompt {len(PROMPT)} chars" in logged
    assert "Do the thing." in logged
    assert "With detail on a second line." not in logged


@pytest.mark.git
async def test_a_long_prompt_first_line_is_bounded_to_80_characters(
    host_repo: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="waystation"):
        await a_run(host_repo, prompt="x" * 500)

    line = next(
        r.getMessage() for r in lifecycle(caplog) if r.getMessage().startswith("agent ")
    )
    assert "x" * 79 in line
    assert "x" * 80 not in line


@pytest.mark.git
async def test_agent_output_is_debug_on_its_own_logger(
    host_repo: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG, logger="waystation"):
        result = await a_run(host_repo)

    output = [r for r in caplog.records if r.name == "waystation.agent.output"]
    messages = [r.getMessage() for r in output]
    assert messages[0] == "working"
    assert messages[-1] == f'{OUTCOME_MARKER} {{"summary": "ok"}}'
    assert {r.levelno for r in output} == {logging.DEBUG}
    assert {run_id_of(r) for r in output} == {result.run_id}
    assert not lifecycle(caplog) or logging.DEBUG not in {
        r.levelno for r in lifecycle(caplog)
    }


@pytest.mark.git
async def test_ctx_log_is_a_logger_adapter_bound_to_the_run(
    host_repo: Path, caplog: pytest.LogCaptureFixture
) -> None:
    seen: list[object] = []

    def note(ctx: RunContext) -> None:
        seen.append(ctx.log)
        ctx.log.info("setup done")

    with caplog.at_level(logging.INFO, logger="waystation"):
        result = await a_run(host_repo).on_sandbox_ready(note)

    assert isinstance(seen[0], logging.LoggerAdapter)
    record = next(r for r in caplog.records if r.name == "waystation.hook")
    assert record.getMessage() == "setup done"
    assert run_id_of(record) == result.run_id
    assert run_name_of(record) is None


def test_command_failed_redacts_its_argv() -> None:
    failure = CommandFailed(
        argv=["docker", "run", "-e", "ANTHROPIC_API_KEY=sk-ant-secret", "image"],
        exit_code=1,
        stderr_tail="boom",
    )

    assert failure.argv == ("docker", "run", "-e", "ANTHROPIC_API_KEY=***", "image")


@pytest.mark.git
async def test_git_and_sandbox_command_lines_are_debug_and_tagged(
    host_repo: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG, logger="waystation"):
        result = await a_run(host_repo)

    commands = [
        r for r in caplog.records if r.name in ("waystation.git", "waystation.sandbox")
    ]
    assert {r.name for r in commands} == {"waystation.git", "waystation.sandbox"}
    assert {r.levelno for r in commands} == {logging.DEBUG}
    assert {run_id_of(r) for r in commands} == {result.run_id}
    assert any("rev-parse" in r.getMessage() for r in commands)


def test_the_console_shows_the_run_name_and_falls_back_to_the_id(
    clean_logging: None, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging()
    run = logging.getLogger("waystation.run")
    run.info("named", extra={"run_id": "0badcafe", "run_name": "tests"})
    run.info("unnamed", extra={"run_id": "0badcafe", "run_name": None})

    err = capsys.readouterr().err
    assert "[tests] named" in err
    assert "[0badcafe] unnamed" in err


def test_redact_argv_sweeps_a_credential_inside_an_argument() -> None:
    argv = (
        "claude",
        "--api-key=sk-ant-api03-Zm9vYmFyYmF6cXV1eA",
        "--env=ANTHROPIC_API_KEY=whatever-this-is",
        "Bearer ghp_0123456789abcdefghijABCDEFGHIJ0123",
    )
    assert redact_argv(argv) == (
        "claude",
        "--api-key=***",
        "--env=ANTHROPIC_API_KEY=***",
        "Bearer ***",
    )


def test_redact_argv_elides_a_token_carried_as_bare_url_userinfo() -> None:
    argv = ("git", "push", "https://ghp_0123456789abcdefghijABCDEFGHIJ@github.com/a/b")
    assert redact_argv(argv) == ("git", "push", "https://***@github.com/a/b")


def test_configure_logging_leaves_a_host_installed_handler_alone(
    clean_logging: None,
) -> None:
    logger = logging.getLogger("waystation")
    theirs = RichHandler(console=Console(file=io.StringIO()))
    logger.addHandler(theirs)

    configure_logging()
    configure_logging()

    installed = [h for h in logger.handlers if isinstance(h, RichHandler)]
    assert theirs in installed
    assert len(installed) == 2


@pytest.mark.git
async def test_a_setup_exec_inside_a_hook_logs_its_output_at_debug(
    host_repo: Path, caplog: pytest.LogCaptureFixture
) -> None:
    async def setup(ctx: RunContext) -> None:
        await ctx.sandbox.exec(["git", "--version"])

    with caplog.at_level(logging.DEBUG, logger="waystation"):
        result = await a_run(host_repo).on_sandbox_ready(setup)

    assert isinstance(result, RunSucceeded)
    sandbox = [r for r in caplog.records if r.name == "waystation.sandbox"]
    assert any(r.getMessage().startswith("git version") for r in sandbox)
    assert {r.levelno for r in sandbox} == {logging.DEBUG}


def test_the_built_in_run_log_is_an_ordinary_hook_bundle() -> None:
    """ADR-0001: nothing waystation watches with is privileged over a user's."""
    registry = HookRegistry().with_bundles(RunLog("0badcafe", None))

    assert {entry.hook for entry in registry.entries} == set(HOOK_NAMES)
