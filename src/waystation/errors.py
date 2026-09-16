"""WaystationError family raised by primitives (ADR-0016)."""

from __future__ import annotations

from waystation.results import AgentExit, Failure, Stage


class WaystationError(Exception):
    """Base for errors raised by primitives; awaited runs never raise these."""


class PreflightError(WaystationError):
    """Host or batch preflight failed before any run started."""

    def __init__(self, message: str, *, failure: Failure | None = None) -> None:
        super().__init__(message)
        self.failure = failure


class StageError(WaystationError):
    """A stage primitive failed; carries the same Failure a RunFailed would."""

    def __init__(
        self,
        stage: Stage,
        failure: Failure,
        *,
        agent: AgentExit | None = None,
    ) -> None:
        self.stage = stage
        self.failure = failure
        self.agent = agent
        super().__init__(f"{stage}: {type(failure).__name__}")
