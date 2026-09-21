"""One run's record: every fact a run gathers, in the one place (#79).

``RunContext`` is the read-only view over it that hooks get, and a result is
assembled from it, so a fact written here once is the fact everything reads —
the base sha a hook sees is the one the result carries, and so is the name.
Only the orchestrator writes it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import TypedDict

from waystation.collect import PatchSeries
from waystation.errors import StageError
from waystation.observability import HOOK, RunLoggerAdapter, package_logger, tag
from waystation.observers import RunLog
from waystation.results import (
    AgentExit,
    IntegrationReport,
    RunConflicted,
    RunFailed,
    RunSucceeded,
    Series,
    Stage,
)
from waystation.sandbox.protocol import Sandbox

__all__ = ["RunRecord", "log_later_failure"]

_logger = package_logger()


def log_unreported(run_id: str, err: StageError, why: str) -> None:
    """Log a failure no result will carry, saying ``why`` it goes unreported."""
    exception = getattr(err.failure, "exception", None)
    _logger.error(
        "run %s: %s failed %s: %r",
        run_id,
        err.stage,
        why,
        err.failure,
        exc_info=exception,
    )


def log_later_failure(run_id: str, err: StageError) -> None:
    """Log a failure met after the run already failed; never report it (ADR-0024)."""
    log_unreported(run_id, err, "after the run had already failed")


class _Facts(TypedDict):
    """The fields every kind of result shares, so ``**`` stays type-checked."""

    run_id: str
    name: str | None
    base_sha: str | None
    elapsed: Mapping[Stage, float]
    agent: AgentExit | None
    series: Series | None
    preserved: str | None


@dataclass(slots=True)
class RunRecord:
    """What a run has gathered so far, and the one failure it will report.

    The bound each stage ran under and the cancellation held until its work
    ended are not here: they are the stage runner's, and this run is its
    first user, not a privileged path (ADR-0032). ``elapsed`` is the runner's
    own live view, handed over as the runner opens.
    """

    run_id: str
    name: str | None
    repo: Path
    prompt: str = ""
    # Where the run is: set by the orchestrator as each stage begins, which
    # the runner cannot do — it never sees the work between stages, reading
    # the prompt or firing a hook. A failure raised there is charged to it,
    # and ``ctx.stage`` reads it, so a hook sees the stage it would fail.
    stage: Stage = "workspace"
    elapsed: Mapping[Stage, float] = field(default_factory=lambda: MappingProxyType({}))
    base_sha: str | None = None
    # The workspace's own branch name, kept here because preservation happens
    # after the workspace is gone; nobody re-derives it (#76).
    branch: str | None = None
    # Live from sandbox_ready until teardown begins, ``None`` either side.
    sandbox: Sandbox | None = None
    agent: AgentExit | None = None
    series: Series | None = None
    patches: PatchSeries | None = None
    preserved: str | None = None
    landed_on: str | None = None
    failure: StageError | None = None
    # The stage a cancellation arrived during, once the runner has held one.
    cancelled_during: Stage | None = None
    log: RunLog = field(init=False)
    hook_log: RunLoggerAdapter = field(init=False)

    def __post_init__(self) -> None:
        self.log = RunLog(self.run_id, self.name)
        self.hook_log = tag(HOOK, self.run_id, self.name)

    def collected(self, series: PatchSeries) -> None:
        """Keep what collect cut, whether or not it went on to refuse it."""
        self.patches = series
        self.series = Series(commits=series.commits, salvaged=series.salvaged)

    def fail(self, err: StageError) -> None:
        """Keep the first failure; log any later one (ADR-0024)."""
        # The net that lets ``failed()`` assert a stage: a failure raised
        # between stages, by a hook say, is claimed by the stage under way.
        err.at(self.stage)
        if self.failure is not None:
            log_later_failure(self.run_id, err)
            return
        self.failure = err
        if err.agent is not None:
            self.agent = err.agent

    def log_cancelled(self) -> None:
        """Say where a cancelled run's series went: no ``run_end`` will."""
        if self.failure is not None:
            log_unreported(self.run_id, self.failure, "in a run that was cancelled")
        stage = self.cancelled_during or self.stage
        self.log.cancelled(stage, kept_on=self.preserved, landed_on=self.landed_on)

    def failed(self) -> RunFailed:
        """The run's failure as its result; the run ends in the stage it failed.

        Owed work goes on after a failure — collect after the agent, say — so
        the stage the run moved on to is not where it ended.
        """
        assert self.failure is not None
        assert self.failure.stage is not None  # fail() attributes every failure
        self.stage = self.failure.stage
        return RunFailed(
            **self._facts(), stage=self.failure.stage, failure=self.failure.failure
        )

    def succeeded[OutcomeT](
        self, outcome: OutcomeT, report: IntegrationReport | None
    ) -> RunSucceeded[OutcomeT]:
        return RunSucceeded(**self._facts(), outcome=outcome, report=report)

    def conflicted[OutcomeT](
        self, outcome: OutcomeT, report: IntegrationReport
    ) -> RunConflicted[OutcomeT]:
        return RunConflicted(**self._facts(), outcome=outcome, report=report)

    def _facts(self) -> _Facts:
        """What every result carries, read off the record once."""
        return _Facts(
            run_id=self.run_id,
            name=self.name,
            base_sha=self.base_sha,
            elapsed=dict(self.elapsed),
            agent=self.agent,
            series=self.series,
            preserved=self.preserved,
        )
