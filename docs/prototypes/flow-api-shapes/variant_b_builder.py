"""PROTOTYPE — throwaway, not runnable (waystation doesn't exist yet).

Variant B of 3: fluent chained builder. `waystation.run(repo)` opens a chain;
each call refines the run; hooks register as chained `.on_*()` methods. The
builder is inert until awaited or handed to `fan_out`. Open question (react!):
does each `.x()` return a copy (safe to fork half-built runs) or mutate self?

Same scenario as variants A and C — see README.md. React, don't run.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from pydantic import BaseModel
from rich.console import Console

import waystation
from waystation import (
    ClaudeCode,
    DockerSandbox,
    RunBuilder,
    RunConflicted,
    RunFailed,
    RunSucceeded,
    fan_out,
)

console = Console()

REPO = Path(__file__).parents[3]  # in-repo self-targeting (primary taught model)

TASKS = [
    "Fix the flaky retry logic in downloader.py",
    "Add type hints to config.py",
    "Delete the dead feature flags in flags.py",
]


class TriageOutcome(BaseModel):
    """Typed Outcome payload each agent must report back."""

    summary: str
    files_touched: list[str]


async def announce_sandbox(ctx) -> None:  # ctx: waystation.RunContext
    console.log(f"[{ctx.run_id}] sandbox up ({ctx.sandbox.backend})")


async def announce_integrated(ctx) -> None:
    console.log(f"[{ctx.run_id}] {len(ctx.report.shas)} commits -> {ctx.report.target}")


def make_run(task: str) -> RunBuilder:
    return (
        waystation.run(REPO)
        .base("main")
        .agent(
            ClaudeCode(
                prompt=f"You are a triage agent. {task}. Commit as you go.",
                outcome_schema=TriageOutcome,
            )
        )
        .sandbox(DockerSandbox(image="waystation-dev"))
        .integrate(target="agents/triage", mechanism="merge")
        .on_sandbox_ready(announce_sandbox)
        .on_integrated(announce_integrated)
    )


async def main() -> None:
    # A single run would just be:  result = await make_run(task)
    async for result in fan_out((make_run(t) for t in TASKS), cap=2):
        match result:
            case RunSucceeded(outcome=TriageOutcome() as o, report=report):
                console.print(f"[green]done[/] {o.summary} ({', '.join(o.files_touched)})")
                if report.salvage:
                    console.print("  [yellow]salvage commit rode along — inspect it[/]")
            case RunConflicted(preserved=branch):
                console.print(f"[red]conflict[/] — series preserved on {branch}")
            case RunFailed(error=err):
                console.print(f"[red]failed[/] {err}")


if __name__ == "__main__":
    asyncio.run(main())
