"""PROTOTYPE — throwaway, not runnable (waystation doesn't exist yet).

Variant C of 3: a shared Flow object. Everything common to the batch (repo,
base, sandbox, integration) lives on one Flow; hooks register ONCE via typed
decorators and apply to every run the flow spawns. Per-run is only what
varies: the agent. `flow.run(agent)` returns the same Run value variant A
builds by hand, so `fan_out` and result handling are unchanged.

Same scenario as variants A and B — see README.md. React, don't run.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from pydantic import BaseModel
from rich.console import Console

from waystation import (
    ClaudeCode,
    DockerSandbox,
    Flow,
    Integration,
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


flow = Flow(
    repo=REPO,
    base="main",
    sandbox=DockerSandbox(image="waystation-dev"),
    integration=Integration(target="agents/triage", mechanism="merge"),
)


@flow.on_sandbox_ready
async def announce_sandbox(ctx) -> None:  # ctx: waystation.RunContext
    console.log(f"[{ctx.run_id}] sandbox up ({ctx.sandbox.backend})")


@flow.on_integrated
async def announce_integrated(ctx) -> None:
    console.log(f"[{ctx.run_id}] {len(ctx.report.shas)} commits -> {ctx.report.target}")


async def main() -> None:
    runs = [
        flow.run(
            ClaudeCode(
                prompt=f"You are a triage agent. {task}. Commit as you go.",
                outcome_schema=TriageOutcome,
            )
        )
        for task in TASKS
    ]

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
