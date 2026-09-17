"""Built-in observers: the hook bundles waystation ships (ADR-0001).

Every observer here is an ordinary bundle — an object with ``on_<hook>``
methods, the same shape a user's own bundle has — so nothing the library
watches with is privileged over what a flow script watches with.

``RunLog`` is the one the orchestrator always carries. It is called directly
rather than registered, so its lines fire in a fixed order and still fire
when a user hook raises; being a bundle is what keeps its vocabulary and the
hook vocabulary the same one.
"""

from __future__ import annotations

from typing import Any, override

from waystation.agents.protocol import AgentLine
from waystation.hooks import HookBundle, RunContext
from waystation.observability import AGENT_OUTPUT, RUN, tag
from waystation.results import (
    AgentExit,
    IntegrationReport,
    RunFailed,
    RunSucceeded,
)

__all__ = ["RunLog"]

# How much of a prompt's first line an INFO line may carry. The rest of the
# prompt never reaches a log record at all.
PROMPT_HEAD = 80


def _head(prompt: str) -> str:
    """The prompt's first line, bounded; never more of it than that."""
    first = prompt.splitlines()[0] if prompt else ""
    return first if len(first) <= PROMPT_HEAD else f"{first[: PROMPT_HEAD - 1]}…"


class RunLog(HookBundle):
    """One run's lifecycle on the console: an INFO line per event, DEBUG output.

    Both loggers are bound to the run, so a line carries its run even when it
    is logged from a task the run's own context never reached.
    """

    __slots__ = ("_output", "_run")

    def __init__(self, run_id: str, name: str | None) -> None:
        self._run = tag(RUN, run_id, name)
        self._output = tag(AGENT_OUTPUT, run_id, name)

    @override
    def on_run_start(self, ctx: RunContext) -> None:
        self._run.info("run start: %s", ctx.repo)

    @override
    def on_workspace_ready(self, ctx: RunContext) -> None:
        self._run.info("workspace ready: base %s", (ctx.base_sha or "")[:12])

    @override
    def on_sandbox_ready(self, ctx: RunContext) -> None:
        self._run.info("sandbox up")

    @override
    def on_agent_output(self, ctx: RunContext, line: AgentLine) -> None:
        prefix = "[stderr] " if line.stream == "stderr" else ""
        self._output.debug("%s%s", prefix, line.raw)

    @override
    def on_agent_end(self, ctx: RunContext, exit: AgentExit) -> None:
        self._run.info("agent end: exit %d in %.1fs", exit.exit_code, exit.elapsed)

    @override
    def on_integrated(self, ctx: RunContext, report: IntegrationReport) -> None:
        landed = len(report.landed)
        self._run.info(
            "integrated: %d commit%s onto %s",
            landed,
            "" if landed == 1 else "s",
            report.target,
        )

    @override
    def on_run_end(
        self, ctx: RunContext, result: RunSucceeded[Any] | RunFailed
    ) -> None:
        elapsed = sum(result.elapsed.values())
        if isinstance(result, RunFailed):
            self._run.info(
                "run end: failed at %s (%s) in %.1fs",
                result.stage,
                type(result.failure).__name__,
                elapsed,
            )
            return
        self._run.info("run end: succeeded in %.1fs", elapsed)

    # The agent's start is the one event with no hook behind it: the agent
    # stage opens between sandbox_ready and the first output line.
    def agent_start(self, prompt: str) -> None:
        """Announce the prompt by shape only — its body is never logged."""
        self._run.info(
            "agent start: prompt %d chars, first line %r", len(prompt), _head(prompt)
        )
