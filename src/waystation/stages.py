"""The stage runner: a run's guarantees, for a loop composed by hand.

The five stages are public primitives so a flow script can compose its own
loop (#18 story 105) — but every guarantee that made a run *safe* used to
live inside the orchestrator, so a hand-written loop quietly went without
them. They live here instead (ADR-0032): a stage's bound, a cancellation
held until that stage's own work has ended (ADR-0017), and how long each
stage took, measured on the clock seam.

Policy is deliberately *not* here. Which stages run, what happens after one
fails, whether a series is preserved, and whether a teardown failure may
change the result all stay with whoever composes — ``RunSpec`` included,
which is the first user of this module rather than a privileged path.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import fields
from functools import partial
from types import MappingProxyType
from typing import Any, Final, Self

from waystation._cancellation import run_to_end, start_bounded
from waystation.clock import get_clock, race_timeout
from waystation.errors import StageError
from waystation.results import Stage, TimedOut, Timeouts

__all__ = ["StageRunner", "stages"]


class _OwnBound:
    """Sentinel: bound a stage by the ``Timeouts`` field of its own name."""

    def __repr__(self) -> str:
        return "<the stage's own bound>"


_OWN: Final = _OwnBound()

# ``Timeouts()`` is frozen and every field defaults to unbounded, so one
# shared instance is the default for every runner that asks for no bounds.
_NO_BOUNDS: Final = Timeouts()


async def _awaited[T](work: Awaitable[T]) -> T:
    """``work`` as a coroutine, which is what a task can be started from."""
    return await work


def _discard(work: Awaitable[Any]) -> None:
    """Close work that will never be awaited, so it warns about nothing."""
    close = getattr(work, "close", None)
    if close is not None:
        close()


class _StageRunner:
    """One run's stages. ``stages()`` makes one; it is private as fan-out's is.

    A flow script names what it calls — ``run.stage(...)`` — never this type.
    """

    def __init__(self, bounds: Timeouts) -> None:
        self._bounds = bounds
        self._elapsed: dict[Stage, float] = {}
        self._held: tuple[Stage, asyncio.CancelledError] | None = None
        self._entered = False
        self._left = False

    async def __aenter__(self) -> Self:
        if self._entered:
            msg = "a stage runner serves one run; call stages() again for the next one"
            raise RuntimeError(msg)
        self._entered = True
        return self

    async def __aexit__(
        self, kind: object, raised: BaseException | None, traceback: object
    ) -> None:
        """End the run, letting a cancellation that is still held go.

        It outranks an exception already unwinding, which becomes its
        ``__context__`` — the rule fan-out's exit follows (ADR-0017).
        """
        self._left = True
        if self._held is not None and raised is not self._held[1]:
            raise self._held[1]

    @property
    def cancelled_during(self) -> Stage | None:
        """The stage a cancellation arrived during, while one is held.

        Only this knows it: by the time a held cancellation surfaces, the
        composer has moved on to the work it does anyway. ``None`` until one
        arrives.
        """
        return None if self._held is None else self._held[0]

    def surface(self) -> None:
        """Raise the held cancellation, if any; do nothing when there is none.

        ``stage`` does this for you before it begins. Call it between stages
        — before firing a hook, say — to stop a run that has been cancelled
        from going any further.

        Raises:
            asyncio.CancelledError: The one that was held.
        """
        if self._held is not None:
            raise self._held[1]

    @property
    def elapsed(self) -> Mapping[Stage, float]:
        """Seconds per stage so far, on the clock seam, however each stage ended.

        A stage run more than once — entering a sandbox and later leaving it,
        say — adds up.
        """
        return MappingProxyType(self._elapsed)

    async def stage[T](
        self,
        stage: Stage,
        work: Awaitable[T],
        *,
        bound: str | None | _OwnBound = _OWN,
        interruptible: bool = False,
    ) -> T:
        """Begin ``stage``: run ``work`` under its bound, and time it.

        A cancellation held from an earlier stage surfaces here first, so the
        next stage never begins. A cancellation arriving *during* ``work`` is
        held until ``work`` has ended and surfaces at the next ``stage`` call
        or when the block ends — a half-cloned workspace, half-cut series or
        half-moved target must never outlive the run that made it (ADR-0017).

        Args:
            stage: The stage this work belongs to: what a failure is
                attributed to, and the key it is timed under.
            work: The stage's own work, typically a primitive's coroutine.
            bound: The ``Timeouts`` field to bound ``work`` by. Defaults to
                the field named by ``stage``; ``None`` is unbounded, which
                the agent stage uses because ``run_agent`` owns silence, wall
                and completion grace itself.
            interruptible: Let a cancellation stop ``work`` instead of
                waiting for it, then hold it and raise it here. The agent
                stage is what this is for, and close to the only thing: its
                exec kills the agent's process tree (ADR-0023), and what the
                agent left is still there to collect, so the composer catches
                this and goes on to collect it. Work that writes — a clone, a
                series, a landing — leaves this alone.

        Returns:
            Whatever ``work`` returned.

        Raises:
            StageError: With ``TimedOut(bound, limit, elapsed)`` when the
                bound fires, as a primitive raises (ADR-0016). A
                ``StageError`` from ``work`` that no stage has claimed comes
                out attributed to ``stage``.
            ValueError: When ``bound`` names no ``Timeouts`` field.
            asyncio.CancelledError: One held from an earlier stage, or — with
                ``interruptible`` — one that stopped ``work`` itself.
        """
        return await self._begin(
            stage, work, bound, surfacing=True, interruptible=interruptible
        )

    async def anyway[T](
        self,
        stage: Stage,
        work: Awaitable[T],
        *,
        bound: str | None | _OwnBound = _OWN,
    ) -> T:
        """Run ``work`` for ``stage`` whatever else has happened.

        The one difference from ``stage``: a cancellation already held does
        not surface first. This is for the work a run owes however it is
        going — leaving a sandbox it entered, collecting what a stopped agent
        left, keeping a series that will reach no target. Skipping any of
        those is how a cancelled run leaks a container or loses work
        (ADR-0017).

        A cancellation arriving during ``work`` is held, as ``stage`` holds one.

        Args:
            stage: The stage this work belongs to; a leaving stays attributed
                to the stage that entered, so a sandbox torn down is still
                the sandbox stage.
            work: The work itself.
            bound: The ``Timeouts`` field to bound it by, the stage's own by
                default; leaving a sandbox passes ``"teardown"``, and
                preservation ``None``.

        Returns:
            Whatever ``work`` returned.

        Raises:
            StageError: With ``TimedOut`` when the bound fires.
            ValueError: When ``bound`` names no ``Timeouts`` field.
        """
        return await self._begin(stage, work, bound, surfacing=False)

    @contextlib.asynccontextmanager
    async def entering[T](
        self, stage: Stage, cm: AbstractAsyncContextManager[T]
    ) -> AsyncIterator[T]:
        """Enter ``cm`` as ``stage``, and leave it when the block ends.

        Entering is bounded by ``stage``; leaving is bounded by ``teardown``,
        attributed to ``stage`` all the same, and happens ``anyway``. The
        block nests rather than running to the runner's own exit, because
        what was entered has to be gone before the stages after it — the
        sandbox before integrate.

        A start that failed is never exited: ``async with`` does not exit a
        context it did not enter. A *leaving* that fails does replace an
        exception the block was already raising, so a composer holding "a
        teardown failure never masks the result" pairs ``stage`` and
        ``anyway`` by hand instead of reaching for this.

        ``cm`` is exited with ``(None, None, None)`` however the block ended,
        so unlike the ``async with`` this stands in for, it never sees the
        exception and cannot suppress one. The shipped backends only release
        what they hold; a context manager that inspects ``__aexit__``'s
        arguments wants its own ``async with`` inside the block.

        Args:
            stage: The stage that owns both halves.
            cm: The context manager to enter — ``backend.start(ws, env={})``.

        Yields:
            Whatever ``cm`` entered as.
        """
        entered = await self.stage(stage, cm.__aenter__())
        try:
            yield entered
        finally:
            await self.anyway(stage, cm.__aexit__(None, None, None), bound="teardown")

    async def _run[T](
        self, stage: Stage, work: Awaitable[T], bound: str | None, interruptible: bool
    ) -> T:
        """``work`` under ``bound``, timed, a cancellation meanwhile held."""
        try:
            seconds = None if bound is None else self._limit(bound)
        except BaseException:
            _discard(work)
            raise
        clock = get_clock()
        started = clock.monotonic()
        task, commits = start_bounded(_awaited(work))
        bounded: Awaitable[T] = race_timeout(task, seconds, commits=commits)
        try:
            if interruptible:
                return await self._stoppable(stage, task, bounded)
            return await run_to_end(bounded, partial(self._hold, stage))
        except TimeoutError as exc:
            if seconds is None:
                raise  # unbounded: this is the work's own, not a bound firing
            assert bound is not None
            raise StageError(
                stage,
                TimedOut(
                    bound=bound, limit=seconds, elapsed=clock.monotonic() - started
                ),
            ) from exc
        except StageError as err:
            err.at(stage)  # attributed once, here, where the stage is run
            raise
        finally:
            self._elapsed[stage] = self._elapsed.get(stage, 0.0) + (
                clock.monotonic() - started
            )

    async def _stoppable[T](
        self, stage: Stage, task: asyncio.Task[T], bounded: Awaitable[T]
    ) -> T:
        """``bounded``, but a cancellation stops it, is held, and is raised here."""
        try:
            return await bounded
        except asyncio.CancelledError as cancel:
            # Awaiting a task does not cancel it, so say so, and wait for it
            # to wind down: the agent's tree dies before the run goes on to
            # collect what it left (ADR-0023).
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            self._hold(stage, cancel)
            raise

    async def _begin[T](
        self,
        stage: Stage,
        work: Awaitable[T],
        bound: str | None | _OwnBound,
        *,
        surfacing: bool,
        interruptible: bool = False,
    ) -> T:
        """What ``stage`` and ``anyway`` share; ``surfacing`` is all they differ by."""
        try:
            self._open()
            if surfacing:
                self.surface()
        except BaseException:
            _discard(work)
            raise
        named = stage if isinstance(bound, _OwnBound) else bound
        return await self._run(stage, work, named, interruptible)

    def _limit(self, bound: str) -> float | None:
        try:
            seconds: float | None = getattr(self._bounds, bound)
        except AttributeError:
            named = ", ".join(field.name for field in fields(Timeouts))
            msg = (
                f"no bound named {bound!r} on Timeouts, whose fields are {named}; "
                "pass bound=None for a stage Timeouts has no field for, as the "
                "agent stage does — run_agent owns its own timers"
            )
            raise ValueError(msg) from None
        return seconds

    def _hold(self, stage: Stage, cancel: asyncio.CancelledError) -> None:
        """Keep the first cancellation until something surfaces it (ADR-0017)."""
        if self._held is None:
            self._held = (stage, cancel)

    def _open(self) -> None:
        if not self._entered:
            msg = "a stage runner runs stages inside `async with stages(...) as run:`"
            raise RuntimeError(msg)
        if self._left:
            msg = "this stage runner's block has ended, and with it its run"
            raise RuntimeError(msg)


type StageRunner = _StageRunner
"""What ``stages()`` returns, for annotating a helper that takes one.

The class itself stays private, as fan-out's iterator does (ADR-0032): an
alias names a runner without making one constructible any way but
``stages()``.
"""


def stages(bounds: Timeouts = _NO_BOUNDS) -> _StageRunner:
    """A stage runner for one hand-composed run: bounds, held cancellations, elapsed.

    What an awaited ``RunSpec`` guarantees, for a loop a flow script writes
    itself (ADR-0032)::

        async with stages(Timeouts(workspace=60, collect=120)) as run:
            ws = await run.stage("workspace", prepare_workspace(repo))
            async with run.entering("sandbox", backend.start(ws, env={})) as box:
                try:
                    exit, outcome = await run.stage(
                        "agent",
                        run_agent(box, agent, prompt, Answer),
                        bound=None,
                        interruptible=True,
                    )
                except asyncio.CancelledError:
                    outcome = None  # stopped; what it committed is still there
                series = await run.anyway("collect", collect(box, ws))
            report = await run.stage("integrate", integrate(repo, series, strategy))

    Each stage runs under the ``Timeouts`` field of its own name, and a bound
    that fires raises ``StageError(stage, TimedOut(...))``. A cancellation
    arriving mid-stage is held until that stage's own work has ended, then
    surfaces at the next ``stage`` call — so the next stage never begins —
    and again when the block ends, where it outranks an exception already
    unwinding. ``anyway`` is the door out of that for the work a run owes
    however it is going, and ``run.elapsed`` reads back the seconds each
    stage took.

    It runs one run: entering the same runner a second time is an error, and
    so is running a stage outside the block. It fires no hooks and logs
    nothing, and it decides no policy: what to do about a failure, whether to
    preserve a series, and whether a teardown failure may change the result
    are the composer's, which is what makes this a tool rather than a
    template to fill in.

    Args:
        bounds: The per-stage bounds. Every field of the default ``Timeouts``
            is unbounded, so a runner asked for nothing bounds nothing
            (ADR-0017).

    Returns:
        A stage runner, to be used as ``async with``.
    """
    return _StageRunner(bounds)
