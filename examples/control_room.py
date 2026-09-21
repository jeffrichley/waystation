"""Rung 5: one prompt across several repos at once, from one control room.

**What it teaches.** ``fan_out`` runs a batch of runs concurrently and hands
you each result as it completes, fastest first. A batch doesn't have to share
anything: here every run comes from a different ``Flow``, one per checkout on
your disk, and they all go into one ``fan_out``.

- **One Flow per checkout.** A ``Flow`` is the defaults for runs against one
  host repo, so several repos means several flows. Building them is a list
  comprehension. There is no registry and no config file.
- **One fan_out for all of them.** It preflights the whole batch first, so a
  missing image or credential stops everything before a single container
  starts. Then it runs them concurrently, at most ``max_concurrency`` at a
  time, and yields results in the order they finish.
- **A failure is one result among many.** ``fan_out`` never raises for a run
  that failed and never cancels the rest: a repo whose run fails is printed,
  and the others carry on.

Each run is named after its checkout with ``.name()``, so the log lines and
the results say which repo they belong to.

**Your HEADs are never touched.** Each repo's work lands on a branch named
``BRANCH`` in that repo. Look at it there, and merge it yourself if you like
it.

**How to run it.** Edit ``CHECKOUTS`` below to list your own repos, and
``PROMPT`` to say what every one of them should get. Build the image and put
a credential in your environment (see examples/README.md), then::

    uv run --group examples --env-file .env python examples/control_room.py

This rung uses no CLI library on purpose: the list is the interface. The
flagship, rung 6, is the one with typer.

**What to watch.** The runs start together, and their log lines interleave,
each tagged with its run id. Results print as they finish, not in list order.
The summary at the end counts what landed, what conflicted, and what failed.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from waystation import (
    ClaudeCode,
    DockerSandbox,
    Flow,
    PreflightError,
    RunConflicted,
    RunFailed,
    RunSucceeded,
    Summary,
    Timeouts,
    configure_logging,
    fan_out,
    handle_signals,
)

# ── Edit these ──────────────────────────────────────────────────────────────
# The checkouts to work on: any git repos on this machine, one run each.
CHECKOUTS = [
    Path("~/code/api").expanduser(),
    Path("~/code/web").expanduser(),
    Path("~/code/infra").expanduser(),
]

# What every checkout gets. A situational prompt, so an inline string.
PROMPT = """\
Read this repository and write AGENT_NOTES.md at its root: five short bullets
saying what the project does and how it is laid out. Change no other file.
Commit AGENT_NOTES.md with a one-line message.
"""

# Where each repo's work lands, in that repo. HEAD stays yours.
BRANCH = "agents/control-room"
# ────────────────────────────────────────────────────────────────────────────


async def main() -> int:
    configure_logging("INFO")
    handle_signals()

    agent = ClaudeCode(model="sonnet", max_budget_usd=1.00)
    sandbox = DockerSandbox("waystation-dev")
    # Every bound defaults to unbounded, so choose your own.
    timeouts = Timeouts(
        workspace=60,
        sandbox=120,
        agent_silence=5 * 60,
        agent_wall=20 * 60,
        collect=60,
        integrate=60,
        teardown=60,
    )
    flows = [
        Flow(checkout, agent=agent, sandbox=sandbox, timeouts=timeouts)
        for checkout in CHECKOUTS
    ]
    batch = [
        flow.run(PROMPT, outcome=Summary).integrate(BRANCH).name(Path(flow.repo).name)
        for flow in flows
    ]

    landed = conflicted = failed = 0
    try:
        # Two at a time: each run is a container and an agent, and your
        # machine and your rate limit are the bound, so pick your own.
        async for result in fan_out(batch, max_concurrency=2):
            match result:
                case RunSucceeded(name=name, outcome=summary):
                    landed += 1
                    print(f"{name}: landed on {BRANCH}: {summary.summary}")
                case RunConflicted(name=name, preserved=preserved):
                    conflicted += 1
                    print(f"{name}: conflicted on {BRANCH}; kept on {preserved}")
                case RunFailed(name=name, stage=stage, failure=failure):
                    failed += 1
                    print(f"{name}: failed at {stage}: {failure!r}")
    except PreflightError as err:
        print(f"preflight: {err}")
        return 1

    print(f"\n{landed} landed, {conflicted} conflicted, {failed} failed")
    return 0 if landed == len(batch) else 1


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except asyncio.CancelledError:
        # handle_signals() cancelled the batch, whose runs have cleaned up.
        # 130 is what a shell reports for a script ended by Ctrl-C.
        sys.exit(130)
