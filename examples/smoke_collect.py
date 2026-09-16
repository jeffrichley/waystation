"""Smoke demo: what waystation does today (collect + preserve + typed failures).

Creates a throwaway host repo, runs three ScriptedAgent flows on NoSandbox, and
prints the typed results plus any ``waystation/<run-id>`` preservation branches.

Requires: git, POSIX sh (Git Bash on Windows). No Docker, no API keys.

Run::

    just smoke
    # or: uv run python examples/smoke_collect.py
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from pydantic import BaseModel

from waystation import (
    AgentExited,
    Flow,
    NoSandbox,
    RunFailed,
    RunSucceeded,
    ScriptedAgent,
    ScriptedCommit,
    Summary,
)


class Answer(BaseModel):
    summary: str


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _init_host(root: Path) -> Path:
    repo = root / "host"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Waystation Smoke")
    _git(repo, "config", "user.email", "smoke@waystation.example")
    (repo / "README").write_text("host baseline\n", encoding="utf-8")
    _git(repo, "add", "README")
    _git(repo, "commit", "-m", "init")
    return repo


def _show_preserved(repo: Path, branch: str | None) -> None:
    if not branch:
        print("  preserved: (none)")
        return
    tip = _git(repo, "rev-parse", "--short", branch)
    subject = _git(repo, "log", "-1", "--format=%s", branch)
    print(f"  preserved: {branch} @ {tip} — {subject}")
    files = _git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", branch)
    for name in files.splitlines():
        if name:
            print(f"    + {name}")


async def _run_cases(host: Path) -> None:
    head_before = _git(host, "rev-parse", "--short", "HEAD")
    print(f"host HEAD before: {head_before}")
    print()

    # 1) Agent commits → series preserved on waystation/<run-id>
    print("=== 1. commit lands on preservation branch ===")
    flow = Flow(
        host,
        agent=ScriptedAgent(
            commits=(
                ScriptedCommit(
                    message="add greeting",
                    files={"hello.txt": "hello from the agent\n"},
                ),
            ),
            outcome=Answer(summary="committed"),
        ),
        sandbox=NoSandbox(),
    )
    result = await flow.run("write a greeting", outcome=Answer)
    match result:
        case RunSucceeded(series=series, preserved=preserved, outcome=outcome):
            print(f"  result: RunSucceeded outcome={outcome!r}")
            print(
                f"  series: commits={series.commits if series else 0} "
                f"salvaged={series.salvaged if series else False}"
            )
            _show_preserved(host, preserved)
        case RunFailed(stage=stage, failure=failure):
            print(f"  unexpected failure: {stage} {failure!r}")
    print(f"  host HEAD still: {_git(host, 'rev-parse', '--short', 'HEAD')}")
    print()

    # 2) Dirty worktree → salvage WIP commit
    print("=== 2. uncommitted work is salvaged ===")
    flow = Flow(
        host,
        agent=ScriptedAgent(
            uncommitted={"scratch.txt": "forgot to commit\n"},
            outcome=Answer(summary="salvaged"),
        ),
        sandbox=NoSandbox(),
    )
    result = await flow.run("leave a dirty file", outcome=Answer)
    match result:
        case RunSucceeded(series=series, preserved=preserved):
            print("  result: RunSucceeded")
            print(
                f"  series: commits={series.commits if series else 0} "
                f"salvaged={series.salvaged if series else False}"
            )
            _show_preserved(host, preserved)
        case RunFailed(stage=stage, failure=failure):
            print(f"  unexpected failure: {stage} {failure!r}")
    print()

    # 3) Non-zero exit → RunFailed, but commits still preserved
    print("=== 3. agent exits non-zero; series still preserved ===")
    flow = Flow(
        host,
        agent=ScriptedAgent(
            commits=(
                ScriptedCommit(
                    message="partial work",
                    files={"partial.txt": "still worth keeping\n"},
                ),
            ),
            outcome=Answer(summary="crashed"),
            exit_code=7,
        ),
        sandbox=NoSandbox(),
    )
    result = await flow.run("crash after committing", outcome=Answer)
    match result:
        case RunFailed(
            stage=stage, failure=failure, series=series, preserved=preserved
        ):
            print(f"  result: RunFailed stage={stage}")
            match failure:
                case AgentExited(exit_code=code, outcome=out):
                    print(f"  failure: AgentExited({code}) outcome={out!r}")
                case _:
                    print(f"  failure: {failure!r}")
            print(f"  series: commits={series.commits if series else 0}")
            _show_preserved(host, preserved)
        case RunSucceeded():
            print("  unexpected success")
    print()

    # 4) Empty series → success, no branch
    print("=== 4. empty series (outcome only) ===")
    flow = Flow(
        host,
        agent=ScriptedAgent(outcome=Summary(summary="nothing to land")),
        sandbox=NoSandbox(),
    )
    result = await flow.run("research only")
    match result:
        case RunSucceeded(series=series, preserved=preserved, outcome=outcome):
            print(f"  result: RunSucceeded outcome={outcome!r}")
            print(f"  series: commits={series.commits if series else 0}")
            print(f"  preserved: {preserved!r}")
        case RunFailed(stage=stage, failure=failure):
            print(f"  unexpected failure: {stage} {failure!r}")
    print()

    print(f"host HEAD after (unchanged): {_git(host, 'rev-parse', '--short', 'HEAD')}")
    branches = [
        b
        for b in _git(host, "for-each-ref", "--format=%(refname:short)").splitlines()
        if b.startswith("waystation/")
    ]
    print(f"preservation branches left behind: {len(branches)}")
    for b in branches:
        print(f"  - {b}")


async def main() -> None:
    # Windows often logs teardown PermissionError; result kind is unchanged.
    logging.getLogger("waystation").setLevel(logging.CRITICAL)
    root = Path(tempfile.mkdtemp(prefix="waystation-smoke-"))
    try:
        host = _init_host(root)
        print(f"throwaway host repo: {host}")
        print()
        await _run_cases(host)
    finally:
        shutil.rmtree(root, ignore_errors=True)
        print()
        print("(throwaway host removed)")


if __name__ == "__main__":
    asyncio.run(main())
