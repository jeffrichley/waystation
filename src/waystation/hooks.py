"""Hooks: lifecycle points, the snapshot registry, and the run context (ADR-0008)."""

from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, get_args

from waystation.agents.protocol import AgentLine
from waystation.errors import StageError
from waystation.observability import HOOK, RunLoggerAdapter, tag, tagged_logger
from waystation.results import (
    AgentExit,
    HookName,
    HookRaised,
    IntegrationReport,
    RunResult,
    Stage,
)
from waystation.sandbox.protocol import Sandbox

_logger = tagged_logger("waystation")

HOOK_NAMES: tuple[HookName, ...] = get_args(HookName)

__all__ = ["HookBundle", "HookName", "RunContext"]


@dataclass(slots=True)
class RunState:
    """The mutable facts behind a ``RunContext``; only the orchestrator writes."""

    run_id: str
    name: str | None
    repo: Path
    prompt: str = ""
    base_sha: str | None = None
    sandbox: Sandbox | None = None
    log: RunLoggerAdapter = field(init=False)

    def __post_init__(self) -> None:
        self.log = tag(HOOK, self.run_id, self.name)


class RunContext:
    """Read-only view of a run, handed to every hook.

    ``base_sha`` is ``None`` until ``workspace_ready``. ``sandbox`` is usable
    from ``sandbox_ready`` until teardown and raises ``RuntimeError`` outside
    that window.
    """

    __slots__ = ("_state",)

    def __init__(self, state: RunState) -> None:
        self._state = state

    @property
    def run_id(self) -> str:
        """The run's id, minted before ``run_start``."""
        return self._state.run_id

    @property
    def name(self) -> str | None:
        """The run's display name, or ``None`` when unnamed."""
        return self._state.name

    @property
    def repo(self) -> Path:
        """The host repo the run targets."""
        return self._state.repo

    @property
    def prompt(self) -> str:
        """The prompt handed to the agent, read from disk if it was a path."""
        return self._state.prompt

    @property
    def base_sha(self) -> str | None:
        """The resolved base ref; ``None`` until ``workspace_ready``."""
        return self._state.base_sha

    @property
    def sandbox(self) -> Sandbox:
        """The live sandbox, from ``sandbox_ready`` until teardown."""
        sandbox = self._state.sandbox
        if sandbox is None:
            msg = "ctx.sandbox is available from sandbox_ready until teardown"
            raise RuntimeError(msg)
        return sandbox

    @property
    def log(self) -> logging.LoggerAdapter[logging.Logger]:
        """A logger already tagged with this run, for a hook's own lines."""
        return self._state.log

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
        stage: Stage,
        ctx: RunContext,
        *args: object,
        stop_on_raise: bool = True,
    ) -> None:
        """Call each function at ``hook`` in order, awaiting async ones.

        A raising function raises ``StageError(stage, HookRaised(...))`` at
        once, or with ``stop_on_raise=False`` for the first one raised after
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
                    stage,
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
