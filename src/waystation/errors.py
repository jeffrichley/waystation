"""WaystationError family raised by primitives (ADR-0016)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from waystation.results import AgentExit, Failure, Stage

if TYPE_CHECKING:
    # collect imports this module, so the type is a name only, never an import.
    from waystation.collect import PatchSeries


class WaystationError(Exception):
    """Base for errors raised by primitives.

    An awaited run returns its failures as a result instead; the one thing it
    raises is a ``PreflightError``, before it begins (ADR-0016).
    """


class PreflightError(WaystationError):
    """Host or batch preflight failed before any run started."""

    def __init__(self, message: str, *, failure: Failure | None = None) -> None:
        super().__init__(message)
        self.failure = failure


class StageError(WaystationError):
    """A stage primitive failed; carries the same Failure a RunFailed would.

    A stage that made something before it failed hands it over on the error,
    so the caller loses nothing: ``agent`` is the agent's exit, and ``series``
    the patches collect cut before refusing them (ADR-0006, ADR-0016).
    """

    def __init__(
        self,
        stage: Stage,
        failure: Failure,
        *,
        agent: AgentExit | None = None,
        series: PatchSeries | None = None,
    ) -> None:
        self.stage = stage
        self.failure = failure
        self.agent = agent
        self.series = series
        super().__init__(f"{stage}: {type(failure).__name__}")
