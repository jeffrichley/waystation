"""Rung 4: an implement run, a review run that gates it, and one bounded fix.

**What it teaches.** A flow is a sequence of runs, and the Outcome of one run
decides what the script does next. Three things come together here:

- **A branch as the meeting point.** The implement run lands its commits on a
  fresh branch, never on your HEAD. Every later run starts from that branch
  with ``.base(branch)``, so each one sees the work so far.
- **A run that integrates nothing.** The review run has no ``.integrate()``:
  its product is its Outcome, a ``Verdict(approved, notes)``, not commits.
  Anything it did commit would be kept on a preservation branch, and never
  land.
- **A typed Outcome as a gate.** ``if verdict.approved`` is ordinary Python.
  On a rejection the script runs one fix, with the reviewer's notes as its
  prompt, landing on the same branch, and then one re-review. The bound is the
  script's own: one fix, not a loop, because an agent that could not satisfy
  the reviewer twice needs a person.

The review instructions are the same on every run, so they live in a file,
``prompts/review.md``, passed as a ``Path``. What is particular to this run,
the task and the commit the work started from, reaches the reviewer through
``.env()``, which the prompt file tells it to read.

**Your HEAD is never touched.** Only the branch this script names moves, so
you can run it in the middle of your own work. When it ends, look at the
branch, and merge it yourself if you like it.

**How to run it.** Build the image and put a credential in your environment
(see examples/README.md), then run it from inside the repo you want worked
on::

    cd path/to/your/repo
    uv run --project path/to/waystation --group examples \\
        --env-file path/to/waystation/.env \\
        python path/to/waystation/examples/implement_then_review.py

Edit ``TASK`` below first: it is what the implement run is asked to do.

**What to watch.** Up to four runs go by, one after another, each with its
own run id in the log: implement, review, and on a rejection fix and review
again. Each review prints its verdict and notes on stdout. At the end,
``git log <branch>`` shows what landed there, and HEAD is where you left it.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

from pydantic import BaseModel

from waystation import (
    ClaudeCode,
    DockerSandbox,
    Flow,
    PreflightError,
    RunConflicted,
    RunFailed,
    RunResult,
    RunSucceeded,
    Summary,
    Timeouts,
    configure_logging,
    handle_signals,
)

# A situational prompt, so an inline string. Change it to your own task.
TASK = """\
Add a short "Contributing" section to the README saying how to run this
project's tests. Keep it to a few lines, and change no other file. Commit it
with a one-line message.
"""

# Reusable instructions, so a file beside this script.
REVIEW_PROMPT = Path(__file__).parent / "prompts" / "review.md"


class Verdict(BaseModel):
    """What the review run reports: the gate, and what to fix if it's shut."""

    approved: bool
    notes: str


def landed(result: RunResult[Summary]) -> bool:
    """Say how a run that should land on the branch went, and whether it did."""
    match result:
        case RunSucceeded(outcome=summary):
            print(f"landed: {summary.summary}")
            return True
        case RunConflicted(preserved=preserved):
            print(f"conflicted landing on the branch; the work is kept on {preserved}")
            return False
        case RunFailed(stage=stage, failure=failure):
            print(f"failed at {stage}: {failure!r}")
            return False


def verdict_of(result: RunResult[Verdict]) -> Verdict | None:
    """The review's Verdict, or ``None`` when the review itself failed."""
    match result:
        case RunSucceeded(outcome=verdict) | RunConflicted(outcome=verdict):
            # A review integrates nothing, so it never conflicts, but the
            # Outcome is the same either way.
            print(f"review: {'approved' if verdict.approved else 'rejected'}")
            print(f"  {verdict.notes}")
            return verdict
        case RunFailed(stage=stage, failure=failure):
            print(f"review failed at {stage}: {failure!r}")
            return None


async def main() -> int:
    configure_logging("INFO")
    handle_signals()

    # A fresh branch per invocation, so a second run of this script never
    # lands on top of the first one's work.
    branch = f"agents/implement-then-review-{time.strftime('%Y%m%d-%H%M%S')}"

    flow = Flow(
        Path.cwd(),
        agent=ClaudeCode(model="sonnet", max_budget_usd=1.00),
        sandbox=DockerSandbox("waystation-dev"),
        # Every bound defaults to unbounded, so choose your own. Each run of
        # the four gets these, since they are the flow's.
        timeouts=Timeouts(
            workspace=60,
            sandbox=120,
            agent_silence=5 * 60,
            agent_wall=20 * 60,
            collect=60,
            integrate=60,
            teardown=60,
        ),
    )

    try:
        implemented = await flow.run(TASK, outcome=Summary).integrate(branch)
        if not landed(implemented):
            return 1
        # The commit the work started from: what the reviewer diffs against.
        base = implemented.base_sha
        assert base is not None, "a run that landed had a base"
        review = (
            flow.run(REVIEW_PROMPT, outcome=Verdict)
            .base(branch)
            .env({"WAYSTATION_TASK": TASK, "WAYSTATION_REVIEW_BASE": base})
        )

        verdict = verdict_of(await review)
        if verdict is None:
            return 1
        if not verdict.approved:
            # The one fix: the reviewer's notes are the prompt, and the fix
            # lands on the same branch, on top of the work it corrects.
            fix = f"{TASK}\nA reviewer rejected the first attempt:\n\n{verdict.notes}"
            fixed = await flow.run(fix, outcome=Summary).base(branch).integrate(branch)
            if not landed(fixed):
                return 1
            # A spec is immutable, and awaiting it again is a new run, which
            # starts from the branch as it is now, fix included.
            verdict = verdict_of(await review)
            if verdict is None:
                return 1
    except PreflightError as err:
        print(f"preflight: {err}")
        return 1

    print(f"\n{branch} {'is approved' if verdict.approved else 'still needs a person'}")
    print(f"  see it with: git log {branch}")
    return 0 if verdict.approved else 1


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except asyncio.CancelledError:
        # handle_signals() cancelled the run, which has already cleaned up.
        # 130 is what a shell reports for a script ended by Ctrl-C.
        sys.exit(130)
