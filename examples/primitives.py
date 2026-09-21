"""Rung 3: the run loop, opened up into its five primitives.

**What it teaches.** ``await flow.run(...)`` is a loop of five stages, and
every stage is a public function you can call yourself:

1. ``prepare_workspace``: clone your committed work at the base ref into a
   private temp directory.
2. ``backend.start(workspace)``: bring the sandbox up around it, and tear it
   down when the block ends.
3. ``run_agent``: run the agent in the sandbox and validate the Outcome it
   reports.
4. ``collect``: cut the agent's commits into a patch series, committing any
   work it left uncommitted first.
5. ``integrate``: land the series on a target with a strategy.

Compose them by hand when the loop you want isn't the one a run gives you:
several agents in one sandbox, a check between collect and integrate, a
series you inspect before deciding where it goes. Each primitive raises
``StageError`` for a failure, carrying the same ``Failure`` value an awaited
run would have returned, so ``err.stage`` and ``err.failure`` are what you
``match`` on here.

**The stage runner keeps the guarantees.** ``async with stages(TIMEOUTS) as
run`` is what makes a hand-written loop as safe as a run. Each
``run.stage(...)`` applies that stage's bound from your ``Timeouts``, and a
Ctrl-C arriving mid-stage is held until the stage's own work has ended, so a
half-cloned workspace or half-moved branch never outlives the loop.
``run.entering(...)`` bounds bringing the sandbox up by ``Timeouts.sandbox``
and tearing it down by ``Timeouts.teardown``. The agent stage passes
``bound=None`` because ``run_agent`` applies the agent's own silence and wall
bounds. It also passes ``interruptible=True``: Ctrl-C then stops the agent
rather than waiting for it, and what it had done is still there to collect.

**What the run did for you, and now you do.** Nothing in this loop decides
policy for you. It is this script that preflights, that keeps the agent's
work on a branch when a Ctrl-C stops it or its landing conflicts
(``preserve_series``), and that removes the workspace at the end. It keeps
nothing when a stage fails, where a run would still collect and keep what
the agent committed: add that if you want it. That is the trade: more lines,
and every one of them yours to change.

**The NoSandbox swap is two lines.** Comment out the ``DockerSandbox`` line
below and uncomment the ``NoSandbox`` one: the same loop then runs Claude
Code on your host, in the workspace clone, with your permissions and your
whole environment. Read ADR-0013 before you do; the sandbox is what makes
``bypassPermissions`` safe.

**This loop lands on a branch, not on your HEAD.** ``Integration(BRANCH)``
moves only ``agents/primitives``, and your checkout stays where it is.

**How to run it.** With the image built and a credential exported (see
examples/README.md), from inside the repo you want the agent to read::

    cd path/to/your/repo
    uv run --project path/to/waystation --group examples \\
        python path/to/waystation/examples/primitives.py

**What to watch.** Stdout prints one line per stage as the loop reaches it,
then the seconds each stage took, which ``run.elapsed`` measured. A failure
names the stage it happened in. ``git log agents/primitives`` shows what
landed.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from pydantic import BaseModel

from waystation import (
    ClaudeCode,
    DockerSandbox,
    Integration,
    PreflightError,
    SandboxBackend,
    StageError,
    Timeouts,
    collect,
    configure_logging,
    handle_signals,
    integrate,
    prepare_workspace,
    preserve_series,
    run_agent,
    stages,
)

BRANCH = "agents/primitives"

AGENT = ClaudeCode(model="sonnet", max_budget_usd=1.00)
SANDBOX: SandboxBackend = DockerSandbox("waystation-dev")
# SANDBOX = NoSandbox()  # and `from waystation import NoSandbox`: on your host

# The stage runner applies the workspace, sandbox, collect, integrate and
# teardown bounds; run_agent applies the two agent ones.
TIMEOUTS = Timeouts(
    workspace=60,
    sandbox=120,
    agent_silence=5 * 60,
    agent_wall=20 * 60,
    collect=60,
    integrate=60,
    teardown=60,
)

PROMPT = """\
Read this repository and write GLOSSARY.md at its root: the five terms a
newcomer most needs, each with a one-line definition. Change no other file.
Commit GLOSSARY.md with a one-line message.
"""


class Notes(BaseModel):
    """The Outcome the agent reports back."""

    summary: str
    files_changed: list[str]


async def main() -> int:
    configure_logging("INFO")
    handle_signals()
    repo = Path.cwd()

    # A run preflights for you; a hand loop asks each piece itself.
    try:
        AGENT.preflight()
        await SANDBOX.preflight()
    except PreflightError as err:
        print(f"preflight: {err}")
        return 1

    async with stages(TIMEOUTS) as run:
        try:
            print("workspace: cloning your committed work")
            ws = await run.stage("workspace", prepare_workspace(repo))
        except StageError as err:
            print(f"failed at {err.stage}: {err.failure!r}")
            return 1
        try:
            print("sandbox: starting")
            async with run.entering("sandbox", SANDBOX.start(ws, env={})) as box:
                print("agent: working")
                try:
                    exit, notes = await run.stage(
                        "agent",
                        run_agent(box, AGENT, PROMPT, Notes, timeouts=TIMEOUTS),
                        bound=None,
                        interruptible=True,
                    )
                except asyncio.CancelledError:
                    # Ctrl-C stopped the agent. Keep what it did, then go on
                    # being cancelled: `anyway` runs even with one held.
                    series = await run.anyway("collect", collect(box, ws))
                    kept = f"waystation/{ws.run_id}"
                    await run.anyway(
                        "integrate",
                        preserve_series(repo, branch=kept, series=series),
                        bound=None,
                    )
                    print(f"cancelled: the agent's work is kept on {kept}")
                    raise
                print(f"collect: agent exited {exit.exit_code}")
                series = await run.stage("collect", collect(box, ws))
            print(f"integrate: {series.commits} commit(s) onto {BRANCH}")
            report = await run.stage(
                "integrate", integrate(repo, series, Integration(BRANCH))
            )
            if report.conflict is not None:
                # A conflict moved nothing. A run would keep the series on a
                # branch of its own; so does this loop.
                kept = f"waystation/{ws.run_id}"
                await run.stage(
                    "integrate",
                    preserve_series(repo, branch=kept, series=series),
                    bound=None,
                )
                paths = ", ".join(report.conflict.paths)
                print(f"conflicted on {paths}: nothing landed; kept on {kept}")
                return 1
        except StageError as err:
            print(f"failed at {err.stage}: {err.failure!r}")
            return 1
        finally:
            await ws.remove()

    print(f"succeeded: {notes.summary}")
    for stage, seconds in run.elapsed.items():
        print(f"  {stage:<10} {seconds:6.1f}s")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except asyncio.CancelledError:
        sys.exit(130)
