"""PROTOTYPE — throwaway, not runnable (waystation doesn't exist yet).

Variant D of 4: hybrid, folding in round-1 reactions. A Flow holds shared
DEFAULTS + flow-level decorator hooks; `flow.run(agent)` opens a fluent
per-run builder that can override any default and add per-run hooks (both
levels fire: flow hooks first, then the run's own). `fan_out` takes any
heterogeneous mix — different agents, sandboxes, integrations — and runs
them all concurrently.

New headline vs A/B/C: the batch is MIXED. Two research agents
(outcome-only, no commits expected) run in parallel with a test-writing
agent that integrates onto its own branch; a second phase then uses the
research findings — sequencing is just plain Python between fan-outs.

React, don't run. See README.md.
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
    RunConflicted,
    RunFailed,
    RunSucceeded,
    fan_out,
)

console = Console()

REPO = Path(__file__).parents[3]  # in-repo self-targeting (primary taught model)


class Findings(BaseModel):
    """Outcome of a research run — report only, no commits."""

    topic: str
    facts: list[str]


class TestReport(BaseModel):
    """Outcome of the test-writing run."""

    tests_added: int
    summary: str


flow = Flow(
    repo=REPO,
    base="main",
    sandbox=DockerSandbox(image="waystation-dev"),  # default; runs may override
)


@flow.on_sandbox_ready  # flow-level: fires for every run this flow spawns
async def announce_sandbox(ctx) -> None:
    console.log(f"[{ctx.run_id}] sandbox up ({ctx.sandbox.backend})")


async def announce_landed(ctx) -> None:  # attached per-run below
    console.log(f"[{ctx.run_id}] {len(ctx.report.shas)} commits -> {ctx.report.target}")


async def main() -> None:
    # ---- phase 1: heterogeneous batch, all parallel -------------------------
    batch = [
        # research runs: outcome-only; no .integrate() (open micro-question:
        # does that mean "skip integration" or "flow/shipped default"?)
        flow.run(ClaudeCode(prompt="Survey how retries fail in downloader.py",
                            outcome_schema=Findings)),
        flow.run(ClaudeCode(prompt="Survey config.py's untyped surface",
                            outcome_schema=Findings)),
        # a working run: per-run overrides + a per-run hook layered on top
        flow.run(ClaudeCode(prompt="Write missing tests for flags.py",
                            outcome_schema=TestReport))
            .sandbox(DockerSandbox(image="waystation-test"))
            .integrate(target="agents/tests", mechanism="apply")
            .on_integrated(announce_landed),
    ]

    findings: list[Findings] = []
    async for result in fan_out(batch):
        match result:
            case RunSucceeded(outcome=Findings() as f):
                console.print(f"[cyan]research[/] {f.topic}: {len(f.facts)} facts")
                findings.append(f)
            case RunSucceeded(outcome=TestReport() as t):
                console.print(f"[green]tests[/] +{t.tests_added}: {t.summary}")
            case RunConflicted(preserved=branch):
                console.print(f"[red]conflict[/] — series preserved on {branch}")
            case RunFailed(error=err):
                console.print(f"[red]failed[/] {err}")

    # ---- phase 2: sequencing is just Python between fan-outs ---------------
    digest = "\n".join(fact for f in findings for fact in f.facts)
    fix = (
        flow.run(ClaudeCode(prompt=f"Fix the issues below. Commit as you go.\n{digest}"))
        .integrate(target="agents/fix", mechanism="merge")
        .on_integrated(announce_landed)
    )
    result = await fix  # a lone builder is simply awaited
    console.print(result)


if __name__ == "__main__":
    asyncio.run(main())
