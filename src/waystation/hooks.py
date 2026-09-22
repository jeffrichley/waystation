"""Hooks: lifecycle points, the snapshot registry, and the run context (ADR-0008)."""

from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, get_args

from waystation.agents.protocol import AgentLine
from waystation.errors import StageError
from waystation.observability import package_logger
from waystation.results import (
    AgentExit,
    HookName,
    HookRaised,
    IntegrationReport,
    RunResult,
    Stage,
)
from waystation.sandbox.protocol import Sandbox

if TYPE_CHECKING:
    from waystation._run_record import RunRecord

_logger = package_logger()

HOOK_NAMES: tuple[HookName, ...] = get_args(HookName)

__all__ = [
    "HOOK_NAMES",
    "HookBundle",
    "HookEntry",
    "HookName",
    "HookRegistry",
    "RunContext",
]


class RunContext:
    """Read-only view of a run, handed to every hook.

    One record sits behind it, and a run's result is assembled from the same
    one, so what a hook reads here is what the result will say (ADR-0039). It is
    live: read again later, it answers for the run as it stands then.

    ``base_sha`` is ``None`` until ``workspace_ready``. ``sandbox`` is usable
    from ``sandbox_ready`` until teardown and raises ``RuntimeError`` outside
    that window.
    """

    __slots__ = ("_record",)

    def __init__(self, record: RunRecord) -> None:
        self._record = record

    @property
    def run_id(self) -> str:
        """The run's id, minted before ``run_start``."""
        return self._record.run_id

    @property
    def name(self) -> str | None:
        """The run's display name, or ``None`` when unnamed."""
        return self._record.name

    @property
    def repo(self) -> Path:
        """The host repo the run targets."""
        return self._record.repo

    @property
    def prompt(self) -> str:
        """The prompt handed to the agent, read from disk if it was a path."""
        return self._record.prompt

    @property
    def base_sha(self) -> str | None:
        """The resolved base ref; ``None`` until ``workspace_ready``."""
        return self._record.base_sha

    @property
    def stage(self) -> Stage:
        """The stage the run is in; a hook raising now fails the run here.

        It moves as each stage begins — to ``integrate`` as the landing
        starts, though no hook fires there. At ``run_end`` it is where the
        run ended: the stage a failed run failed in, even when work it still
        owed, such as collecting, went on after.
        """
        return self._record.stage

    @property
    def elapsed(self) -> Mapping[Stage, float]:
        """Seconds per stage so far, for each stage whose work has ended.

        Read-only and live, measured on the clock seam: a stage appears once
        its work has ended, and a stage run twice — a sandbox entered and
        then left — adds up. The result's ``elapsed`` is this, at the end.
        """
        return self._record.elapsed

    @property
    def sandbox(self) -> Sandbox:
        """The live sandbox, from ``sandbox_ready`` until teardown."""
        sandbox = self._record.sandbox
        if sandbox is None:
            msg = "ctx.sandbox is available from sandbox_ready until teardown"
            raise RuntimeError(msg)
        return sandbox

    @property
    def log(self) -> logging.LoggerAdapter[logging.Logger]:
        """A logger already tagged with this run, for a hook's own lines."""
        return self._record.hook_log

    def __repr__(self) -> str:
        return f"RunContext(run_id={self.run_id!r}, name={self.name!r})"


class HookBundle:
    """Optional base for hook bundles: every hook is a no-op until overridden.

    Any object with some of the ``on_<hook>`` methods is already a bundle.
    Subclassing adds signature checking, and marking each override with
    ``typing.override`` lets a type checker catch a misspelled hook name.
    Overrides may be sync or async.
    """

    def on_run_start(self, ctx: RunContext) -> Awaitable[None] | None:
        """Fired as a run starts, before the workspace stage."""
        return None

    def on_workspace_ready(self, ctx: RunContext) -> Awaitable[None] | None:
        """Fired once the workspace is prepared."""
        return None

    def on_sandbox_ready(self, ctx: RunContext) -> Awaitable[None] | None:
        """Fired once the sandbox is up, before the agent starts."""
        return None

    def on_agent_output(
        self, ctx: RunContext, line: AgentLine
    ) -> Awaitable[None] | None:
        """Fired for each line the agent emits."""
        return None

    def on_agent_end(self, ctx: RunContext, exit: AgentExit) -> Awaitable[None] | None:
        """Fired when the agent exec ends; ``exit.exit_code`` is -1 if stopped."""
        return None

    def on_integrated(
        self, ctx: RunContext, report: IntegrationReport
    ) -> Awaitable[None] | None:
        """Fired when integration lands."""
        return None

    def on_run_end(
        self,
        ctx: RunContext,
        result: RunResult[Any],
    ) -> Awaitable[None] | None:
        """Fired for every result a run returns."""
        return None


@dataclass(frozen=True, slots=True)
class HookEntry:
    """One registered function at one hook point."""

    hook: HookName
    function: Callable[..., object]


def _describe(function: Callable[..., object]) -> str:
    qualname = getattr(function, "__qualname__", None)
    return qualname if isinstance(qualname, str) else type(function).__qualname__


@dataclass(frozen=True, slots=True)
class HookRegistry:
    """Ordered hook registrations; a run fires the snapshot it was built with."""

    entries: tuple[HookEntry, ...] = ()

    def with_bundles(self, *bundles: object) -> HookRegistry:
        """Append every ``on_<hook>`` method each bundle defines."""
        added: list[HookEntry] = []
        for bundle in bundles:
            found = [
                HookEntry(hook, getattr(bundle, f"on_{hook}"))
                for hook in HOOK_NAMES
                if callable(getattr(bundle, f"on_{hook}", None))
            ]
            if not found:
                msg = (
                    f"hook bundle {bundle!r} defines no on_<hook> method; "
                    f"expected any of {', '.join(f'on_{h}' for h in HOOK_NAMES)}"
                )
                raise TypeError(msg)
            added.extend(found)
        return HookRegistry((*self.entries, *added))

    def with_function(
        self, hook: HookName, function: Callable[..., object]
    ) -> HookRegistry:
        """Append one function at ``hook``."""
        return HookRegistry((*self.entries, HookEntry(hook, function)))

    async def fire(
        self,
        hook: HookName,
        ctx: RunContext,
        *args: object,
        stop_on_raise: bool = True,
    ) -> None:
        """Call each function at ``hook`` in order, awaiting async ones.

        A raising function raises ``StageError(ctx.stage, HookRaised(...))``
        at once, or with ``stop_on_raise=False`` for the first one raised after
        every other function at ``hook`` has run; later ones are logged
        (ADR-0024).
        """
        first: StageError | None = None
        for entry in self.entries:
            if entry.hook != hook:
                continue
            try:
                returned = entry.function(ctx, *args)
                if inspect.isawaitable(returned):
                    await returned
            except Exception as exc:
                error = StageError(
                    ctx.stage,
                    HookRaised(
                        hook=hook,
                        function=_describe(entry.function),
                        exception=exc,
                    ),
                )
                if stop_on_raise:
                    raise error from exc
                if first is None:
                    error.__cause__ = exc
                    first = error
                else:
                    _logger.error(
                        "run %s: %s hook %s raised after another did: %r",
                        ctx.run_id,
                        hook,
                        _describe(entry.function),
                        exc,
                        exc_info=exc,
                    )
        if first is not None:
            raise first
