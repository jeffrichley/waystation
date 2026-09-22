"""Patches land byte for byte: no newline translation on any host (Windows)."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from pydantic import BaseModel

from helpers import OK_OUTCOME_LINE, ShellAgent, commit_on, git, git_bytes, printf_bytes
from waystation import (
    Flow,
    Integration,
    NoSandbox,
    PatchSeries,
    Refused,
    RunFailed,
    RunSucceeded,
)
from waystation.integration import GitRepo
from waystation.testing import ScriptedAgent, ScriptedCommit


class Answer(BaseModel):
    summary: str


def _agent() -> ScriptedAgent:
    return ScriptedAgent(
        commits=(
            ScriptedCommit(
                message="edit notes\n\nwhy it changed",
                files={"notes.txt": "edited\n", "new.txt": "fresh\n"},
            ),
        ),
        outcome=Answer(summary="ok"),
    )


@pytest.mark.git
@pytest.mark.asyncio
@pytest.mark.parametrize("integrated", [True, False])
async def test_series_reaches_the_host_byte_for_byte(
    host_repo: Path, integrated: bool
) -> None:
    flow = Flow(host_repo, agent=_agent(), sandbox=NoSandbox())
    spec = flow.run("edit", outcome=Answer)
    if integrated:
        spec = spec.integrate("agents/bytes")

    result = await spec

    assert isinstance(result, RunSucceeded)
    ref = "agents/bytes" if integrated else f"waystation/{result.run_id}"
    assert git_bytes(host_repo, "cat-file", "blob", f"{ref}:notes.txt") == b"edited\n"
    assert git_bytes(host_repo, "cat-file", "blob", f"{ref}:new.txt") == b"fresh\n"
    message = git_bytes(host_repo, "log", "-1", "--format=%B", ref)
    assert b"\r" not in message
    assert message.startswith(b"edit notes\n\nwhy it changed")


@pytest.mark.git
async def test_carriage_returns_in_content_survive_collect(host_repo: Path) -> None:
    # The series leaves the sandbox on the same exec as collect's checks,
    # after its verdict line (ADR-0029): that must cost it no byte. printf
    # makes the CRs: a CR in a Windows command line does not reach Git
    # Bash's sh.
    agent = ShellAgent(
        "printf '*.crlf -text\\n' > .gitattributes && "
        "printf 'one\\r\\ntwo\\r\\n' > dos.crlf && "
        f"git add -A && git commit -qm dos && echo '{OK_OUTCOME_LINE}'"
    )

    result = await Flow(host_repo, agent=agent, sandbox=NoSandbox()).run("dos")

    assert isinstance(result, RunSucceeded), result
    kept = f"waystation/{result.run_id}:dos.crlf"
    assert git_bytes(host_repo, "cat-file", "blob", kept) == b"one\r\ntwo\r\n"


@pytest.mark.git
@pytest.mark.asyncio
async def test_carriage_returns_in_content_survive_from_range(
    host_repo: Path,
) -> None:
    (host_repo / ".gitattributes").write_bytes(b"*.crlf -text\n")
    git(host_repo, "add", ".gitattributes")
    git(host_repo, "commit", "-m", "attributes")
    base = git(host_repo, "rev-parse", "HEAD")
    commit_on(host_repo, "source", {"dos.crlf": "one\r\ntwo\r\n"})

    series = await PatchSeries.from_range(host_repo, base, "source")
    report = await Integration("agents/dos").integrate(
        await GitRepo.open(host_repo), series
    )

    assert report.target_after is not None
    landed = git_bytes(host_repo, "cat-file", "blob", "agents/dos:dos.crlf")
    assert landed == b"one\r\ntwo\r\n"


LATIN_1 = b"caf\xe9\n\xff\n"
"""Text that is not UTF-8: git calls it text, so a patch carries it raw.

Only a file with a NUL is binary to git, and goes into a patch as base85.
"""


@pytest.mark.git
async def test_text_that_is_not_utf8_survives_collect(host_repo: Path) -> None:
    agent = ShellAgent(
        f"{printf_bytes('latin1.txt', LATIN_1)} && "
        f"git add -A && git commit -qm latin1 && echo '{OK_OUTCOME_LINE}'"
    )

    result = await Flow(host_repo, agent=agent, sandbox=NoSandbox()).run("latin-1")

    assert isinstance(result, RunSucceeded), result
    kept = f"waystation/{result.run_id}:latin1.txt"
    assert git_bytes(host_repo, "cat-file", "blob", kept) == LATIN_1


@pytest.mark.git
async def test_text_that_is_not_utf8_is_logged_readable(
    host_repo: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # The series exec is setup, so its lines are logged at DEBUG (issue #6).
    # The patch keeps its bytes; the log is for people, and a lone surrogate
    # would break any handler that encodes strictly.
    agent = ShellAgent(
        f"{printf_bytes('latin1.txt', LATIN_1)} && "
        f"git add -A && git commit -qm latin1 && echo '{OK_OUTCOME_LINE}'"
    )

    with caplog.at_level(logging.DEBUG, logger="waystation.sandbox"):
        result = await Flow(host_repo, agent=agent, sandbox=NoSandbox()).run("log")

    assert isinstance(result, RunSucceeded), result
    logged = [r.getMessage() for r in caplog.records if r.name == "waystation.sandbox"]
    assert "+caf\N{REPLACEMENT CHARACTER}" in logged


@pytest.mark.git
async def test_text_that_is_not_utf8_survives_a_squashed_series(
    host_repo: Path,
) -> None:
    # A merge makes the series nonlinear, so collect squashes it (ADR-0006):
    # its diff leaves the sandbox on one exec's stdout and goes back in on
    # another's stdin.
    agent = ShellAgent(
        f"git checkout -qb side && {printf_bytes('latin1.txt', LATIN_1)} && "
        "git add -A && git commit -qm latin1 && git checkout -q - && "
        f"git merge -q --no-ff side -m merged && echo '{OK_OUTCOME_LINE}'"
    )

    result = await Flow(host_repo, agent=agent, sandbox=NoSandbox()).run("merge")

    assert isinstance(result, RunFailed), result
    assert isinstance(result.failure, Refused), result.failure
    kept = f"{result.preserved}:latin1.txt"
    assert git_bytes(host_repo, "cat-file", "blob", kept) == LATIN_1
