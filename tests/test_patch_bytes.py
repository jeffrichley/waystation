"""Patches land byte for byte: no newline translation on any host (Windows)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from pydantic import BaseModel

from helpers import commit_on, git
from waystation import (
    Flow,
    Integration,
    NoSandbox,
    PatchSeries,
    RunSucceeded,
    ScriptedAgent,
    ScriptedCommit,
)
from waystation.integration import GitRepo


class Answer(BaseModel):
    summary: str


def _bytes(repo: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True
    ).stdout


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
    assert _bytes(host_repo, "cat-file", "blob", f"{ref}:notes.txt") == b"edited\n"
    assert _bytes(host_repo, "cat-file", "blob", f"{ref}:new.txt") == b"fresh\n"
    message = _bytes(host_repo, "log", "-1", "--format=%B", ref)
    assert b"\r" not in message
    assert message.startswith(b"edit notes\n\nwhy it changed")


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
    landed = _bytes(host_repo, "cat-file", "blob", "agents/dos:dos.crlf")
    assert landed == b"one\r\ntwo\r\n"
