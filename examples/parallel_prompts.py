"""Rung 6: the flagship. Fan prompts out onto one batch branch, resolve conflicts.

**What it teaches.** This is a whole product in one script: a command line
that takes any number of prompts, runs one Claude Code agent per prompt, each
in its own container, and lands every agent's commits on one shared batch
branch while a live dashboard shows the batch. Nothing in it is new
machinery. It is rungs 1 to 5 put together, plus one idea: **a conflict is
resolved by another run.**

Agents working at the same time on one branch will sometimes change the same
lines. Waystation never guesses how to merge them. The run whose commits
would not land returns ``RunConflicted``, nothing it did is lost (its commits
sit on the preservation branch it names), and the script decides what
happens next. Here, it launches a **resolver run** the moment the conflict
comes back, while the rest of the batch keeps going. A resolver run is an
ordinary run with three differences, all spelled out below:

- its workspace starts at the batch branch, as it stands now: ``.base(target)``;
- the preservation branch travels into its workspace too: ``.extra_refs(...)``;
- its prompt, from ``prompts/resolve.md``, tells the agent to replay the
  conflicted series with ``git cherry-pick base..preserved`` and never merge,
  so the batch branch stays a straight line of commits.

A resolver can conflict too, if the branch moved again while it worked. It
is retried up to ``--resolve-attempts`` times, each time from its own
preservation branch. After the last one, the script prints the branch the
work is kept on and carries on. A failed run is printed, and the batch
carries on too: one result never stops the others.

**Where the work lands.** On ``agents/<name>`` by default, a branch the
first landing creates, so your checkout is never touched. ``--target HEAD``
lands on your own checkout instead (see rung 1's loud note first). For one
branch per run rather than one shared branch, change the one line marked
``per-run branches`` below by appending
``.integrate(f"{target}-{run_name(prompt)}")``. Then each run gets a branch of
its own, nothing can conflict, and nothing is resolved.

**How to run it.** Build the image, export a credential (or use
``--env-file``, see ``examples/README.md``), then, from inside the repo the
agents should work on::

    uv run --project path/to/waystation --group examples \\
        python path/to/waystation/examples/parallel_prompts.py \\
        "add a --verbose flag to the CLI" \\
        "write tests for the parser" \\
        docs/prompts/refactor-config.md \\
        --agents 2 --name tidy-up

Each prompt is a string or the path of an existing ``.md`` file. A run is
named after the file, or after the first four words of the string, and that
name labels its dashboard row and its log file. ``--help`` lists every flag.

**What to watch.** The dashboard: one row per run, with its stage, the last
thing its agent said, what it has cost, and a glyph once it ends: ``✓``
landed, ``!`` conflicted, ``✗`` failed. A conflicted row is followed by a
``resolve-...`` row a moment later. Each run writes a file under
``logs/<name>/`` that ``tail -f`` can follow. When the batch is done,
``git log agents/<name>`` shows every landed commit, one straight line.
"""

from __future__ import annotations

import asyncio
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from waystation import (
    ClaudeCode,
    Dashboard,
    DockerSandbox,
    Flow,
    Integration,
    PreflightError,
    RunConflicted,
    RunFailed,
    RunLogFiles,
    RunSucceeded,
    Summary,
    Timeouts,
    configure_logging,
    fan_out,
    handle_signals,
)

# Reusable instructions, so a file. `{base}`, `{preserved}` and `{target}`
# are filled in for each conflict.
RESOLVE_PROMPT = Path(__file__).parent / "prompts" / "resolve.md"

# Every bound defaults to unbounded, so choose your own. An agent silent for
# five minutes is stuck; half an hour is a generous ceiling for one prompt.
TIMEOUTS = Timeouts(
    workspace=60,
    sandbox=120,
    agent_silence=5 * 60,
    agent_wall=30 * 60,
    collect=60,
    integrate=60,
    teardown=60,
)


def prompt_of(arg: str) -> str | Path:
    """A path when ``arg`` names an existing ``.md`` file, else the prompt itself."""
    path = Path(arg)
    return path if path.suffix == ".md" and path.is_file() else arg


def run_name(prompt: str | Path) -> str:
    """The file's stem, or the first four words of the prompt, slugified."""
    if isinstance(prompt, Path):
        return prompt.stem
    words = " ".join(prompt.split()[:4])
    return re.sub(r"[^a-z0-9]+", "-", words.lower()).strip("-") or "prompt"


async def resolve(
    flow: Flow,
    conflicted: RunConflicted[Summary],
    *,
    name: str,
    target: str,
    attempts: int,
    console: Console,
) -> None:
    """Resolver runs for one conflict, until one lands or ``attempts`` run out."""
    for attempt in range(1, attempts + 1):
        preserved, base = conflicted.preserved, conflicted.base_sha
        if preserved is None or base is None:
            break
        prompt = RESOLVE_PROMPT.read_text(encoding="utf-8").format(
            base=base, preserved=preserved, target=target
        )
        label = f"resolve-{name}-{attempt}"
        result = await (
            flow.run(prompt, outcome=Summary)
            .name(label)
            .base(target)
            .extra_refs(preserved)
        )
        match result:
            case RunSucceeded(outcome=outcome):
                console.print(f"[green]resolved[/] {name}: {outcome.summary}")
                return
            case RunConflicted():
                conflicted = result
            case RunFailed(stage=stage, failure=failure, preserved=kept):
                console.print(
                    f"[red]resolver failed[/] {label} at {stage}: {failure!r}"
                )
                if kept is not None:
                    console.print(f"  its work is kept on {kept}")
                console.print(f"  {name}'s work is still on {preserved}")
                return
    console.print(
        f"[yellow]unresolved[/] {name}: its work is kept on {conflicted.preserved}"
    )


async def batch(
    prompts: list[str | Path],
    *,
    base: str,
    agents: int | None,
    target: str,
    image: str,
    name: str,
    resolve_attempts: int,
) -> int:
    console = configure_logging("INFO")
    handle_signals()
    console.rule(f"batch {name} → {target}")

    failed = 0
    with RunLogFiles(Path("logs") / name) as files:
        async with Dashboard() as dashboard, asyncio.TaskGroup() as resolvers:
            flow = Flow(
                Path.cwd(),
                agent=ClaudeCode(),
                sandbox=DockerSandbox(image),
                base=base,
                timeouts=TIMEOUTS,
                # Every run lands on one branch, commit by commit (`apply`).
                integration=Integration(target),
                # Every run, resolvers included, gets a row and a log file.
                hooks=[dashboard, files],
            )
            runs = (
                flow.run(prompt, outcome=Summary).name(run_name(prompt))
                # per-run branches: append .integrate(f"{target}-{run_name(prompt)}")
                for prompt in prompts
            )
            async with fan_out(runs, max_concurrency=agents) as results:
                async for result in results:
                    match result:
                        case RunSucceeded(name=run, outcome=outcome):
                            console.print(f"[green]landed[/] {run}: {outcome.summary}")
                        case RunConflicted(name=run, preserved=preserved):
                            console.print(
                                f"[yellow]conflict[/] {run}, kept on {preserved}"
                            )
                            # Launched now, not after the batch: it runs beside
                            # the runs still going.
                            resolvers.create_task(
                                resolve(
                                    flow,
                                    result,
                                    name=run or result.run_id,
                                    target=target,
                                    attempts=resolve_attempts,
                                    console=console,
                                )
                            )
                        case RunFailed(name=run, stage=stage, failure=failure):
                            failed += 1
                            console.print(
                                f"[red]failed[/] {run} at {stage}: {failure!r}"
                            )
    return 1 if failed else 0


def main(
    prompts: Annotated[
        list[str], typer.Argument(help="Prompts: each a string or a .md file.")
    ],
    base: Annotated[str, typer.Option(help="The ref every run starts from.")] = "HEAD",
    agents: Annotated[
        int | None, typer.Option(help="Most runs at once; unlimited if unset.")
    ] = None,
    target: Annotated[
        str | None,
        typer.Option(help="Where the work lands: a branch, or HEAD. [agents/<name>]"),
    ] = None,
    image: Annotated[str, typer.Option(help="The sandbox image.")] = "waystation-dev",
    name: Annotated[
        str | None,
        typer.Option(help="The batch's name, for its branch, logs and dashboard."),
    ] = None,
    resolve_attempts: Annotated[
        int, typer.Option(help="Resolver runs per conflict before giving up.")
    ] = 1,
) -> None:
    """Fan prompts out to Claude Code agents, landing on one batch branch."""
    name = name or datetime.now().strftime("batch-%Y%m%d-%H%M%S")
    try:
        code = asyncio.run(
            batch(
                [prompt_of(arg) for arg in prompts],
                base=base,
                agents=agents,
                target=target or f"agents/{name}",
                image=image,
                name=name,
                resolve_attempts=resolve_attempts,
            )
        )
    except PreflightError as err:
        print(f"preflight: {err}", file=sys.stderr)
        raise typer.Exit(1) from None
    except asyncio.CancelledError:
        # handle_signals() cancelled the batch; every run has cleaned up.
        raise typer.Exit(130) from None
    raise typer.Exit(code)


if __name__ == "__main__":
    typer.run(main)
