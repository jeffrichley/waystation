"""Smoke demo: land an agent series onto a named branch (issue #23).

Creates a throwaway host repo, runs one ScriptedAgent flow with
``Integration("agents/demo")``, and shows the typed result plus the
target branch — without touching HEAD.

Swap in ``Squash("agents/demo")`` to land the run's whole series as one
commit instead; ``Integration(..., mechanism="merge")`` lands it with a merge
commit. All three are built from ``GitRepo``'s public landing steps, which a
strategy of your own can use too (ADR-0040).

Requires: git ≥ 2.40, POSIX sh (Git Bash on Windows). No Docker, no API keys.

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
    Flow,
    Integration,
    NoSandbox,
    RunFailed,
    RunSucceeded,
    Timeouts,
    configure_logging,
    handle_signals,
)
from waystation.testing import ScriptedAgent, ScriptedCommit


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


async def main() -> None:
    # One rich handler on stderr, so this demo's own prints keep stdout.
    configure_logging("INFO")
    # Ctrl-C or SIGTERM cancels this task; the run still keeps its series.
    handle_signals()
    # Windows often cannot unlink git's read-only objects at teardown. The
    # run's result is unchanged, so keep that one line out of the demo.
    logging.getLogger("waystation.sandbox").setLevel(logging.CRITICAL)
    root = Path(tempfile.mkdtemp(prefix="waystation-smoke-"))
    try:
        host = _init_host(root)
        head = _git(host, "rev-parse", "--short", "HEAD")
        print(f"throwaway host repo: {host}")
        print(f"host HEAD: {head}")
        print()

        print("=== land ScriptedAgent commits onto agents/demo (apply) ===")
        flow = Flow(
            host,
            agent=ScriptedAgent(
                commits=(
                    ScriptedCommit(
                        message="add greeting",
                        files={"hello.txt": "hello from the agent\n"},
                    ),
                ),
                outcome=Answer(summary="landed"),
            ),
            sandbox=NoSandbox(),
            integration=Integration("agents/demo"),
            # Every bound defaults to unbounded (ADR-0017), so choose your
            # own. A scripted agent in a local repo finishes in a second;
            # these only give a wedged git a way out.
            timeouts=Timeouts(
                workspace=60,
                sandbox=60,
                agent_silence=60,
                agent_wall=120,
                collect=60,
                integrate=60,
                teardown=60,
            ),
        )
        result = await flow.run("write a greeting", outcome=Answer)

        match result:
            case RunSucceeded(
                series=series,
                preserved=preserved,
                outcome=outcome,
                report=report,
            ):
                print(f"  result:    RunSucceeded outcome={outcome!r}")
                commits = series.commits if series else 0
                salvaged = series.salvaged if series else False
                print(f"  series:    commits={commits} salvaged={salvaged}")
                print(f"  preserved: {preserved!r}  (None = integrated)")
                if report is not None:
                    after = report.target_after[:8] if report.target_after else None
                    print(f"  report:    target={report.target}")
                    print(f"             mechanism={report.mechanism}")
                    print(f"             before={report.target_before[:8]}…")
                    print(f"             after={after}…")
                    print(f"             landed={len(report.landed)} commit(s)")
                head_now = _git(host, "rev-parse", "--short", "HEAD")
                tip = _git(host, "log", "-1", "--oneline", "agents/demo")
                body = _git(host, "show", "agents/demo:hello.txt")
                print(f"  host HEAD still: {head_now}")
                print(f"  agents/demo tip: {tip}")
                print(f"  hello.txt on branch: {body!r}")
            case RunFailed(stage=stage, failure=failure):
                print(f"  unexpected failure: {stage} {failure!r}")
    finally:
        shutil.rmtree(root, ignore_errors=True)
        print()
        print("(throwaway host removed)")


if __name__ == "__main__":
    asyncio.run(main())
