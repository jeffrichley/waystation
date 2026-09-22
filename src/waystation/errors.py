"""WaystationError family raised by primitives (ADR-0016)."""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import TYPE_CHECKING

from waystation.results import AgentExit, Failure, Stage

__all__ = ["PreflightError", "StageError", "WaystationError", "attributing"]

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
        """Say why preflight failed.

        Args:
            message: What failed, for a person to read.
            failure: The failure behind it, when a check has one to hand on.
        """
        super().__init__(message)
        self.failure = failure


class StageError(WaystationError):
    """A stage primitive failed; carries the same Failure a RunFailed would.

    A stage that made something before it failed hands it over on the error,
    so the caller loses nothing: ``agent`` is the agent's exit, and ``series``
    the patches collect cut before refusing them (ADR-0006, ADR-0016).

    ``stage`` is ``None`` while nothing has claimed the failure yet. Helpers
    below a primitive — host git, a range read off the host — raise that way,
    because which stage a failed ``rev-parse`` belongs to is known where the
    stage is run, not where git is (ADR-0032). ``at`` is where it is decided.
    """

    def __init__(
        self,
        stage: Stage | None,
        failure: Failure,
        *,
        agent: AgentExit | None = None,
        series: PatchSeries | None = None,
    ) -> None:
        """Carry ``failure``, and whatever the stage made before it failed.

        Args:
            stage: The stage that failed, or ``None`` while unattributed.
            failure: What went wrong, as a ``RunFailed`` would report it.
            agent: The agent's exit, when the agent had run.
            series: The patches collect cut before it refused them.
        """
        self.stage = stage
        self.failure = failure
        self.agent = agent
        self.series = series
        super().__init__(f"{stage or 'unattributed'}: {type(failure).__name__}")

    def at(self, stage: Stage) -> StageError:
        """Attribute this failure to ``stage``, unless a stage already owns it.

        Attribution happens once, and the first claim wins: a primitive names
        its own stage at its edge, and whoever runs that primitive — a stage
        runner, or an orchestrator — leaves the name it finds alone.

        Args:
            stage: The stage to attribute an unattributed failure to.

        Returns:
            This error, so a caller can ``raise err.at(stage)``.
        """
        if self.stage is None:
            self.stage = stage
            self.args = (f"{stage}: {type(self.failure).__name__}",)
        return self


@contextlib.contextmanager
def attributing(stage: Stage) -> Iterator[None]:
    """Attribute any unattributed ``StageError`` leaving this block to ``stage``.

    A primitive's edge: inside it, helpers raise failures with no stage on
    them, and this is the one place that says which stage they happened in
    (ADR-0032).

    Module-public rather than underscore-private because the primitives that
    use it live in other modules — workspace, collect, integration and the
    sandbox transport — and reaching across modules for a private name is
    what the house rule forbids. It stays out of top-level ``waystation``:
    a flow script attributes with ``StageError.at`` or a stage runner.

    Args:
        stage: The stage to attribute a failure to.

    Raises:
        StageError: The one that left the block, attributed.
    """
    try:
        yield
    except StageError as err:
        err.at(stage)
        raise
