"""PROTOTYPE — throwaway, not runnable (waystation doesn't exist yet).

Variant A of 3: plain data + free functions. A run is a value object built
with keyword args; hooks are just another field. Barest possible shape —
no chaining, no shared state, everything inspectable before it starts.

Same scenario as variants B and C — see README.md. React, don't run.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from pydantic import BaseModel
from rich.console import Console

from waystation import (
    ClaudeCode,
    DockerSandbox,
    Hooks,
    Integration,
    Run,
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


def make_run(task: str) -> Run:
    return Run(
        repo=REPO,
        base="main",
        agent=ClaudeCode(
            prompt=f"You are a triage agent. {task}. Commit as you go.",
            outcome_schema=TriageOutcome,
        ),
        sandbox=DockerSandbox(image="waystation-dev"),
        integration=Integration(target="agents/triage", mechanism="merge"),
        hooks=Hooks(
            on_sandbox_ready=announce_sandbox,
            on_integrated=announce_integrated,
        ),
    )


async def main() -> None:
    runs = [make_run(t) for t in TASKS]

    async for result in fan_out(runs, cap=2):
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
