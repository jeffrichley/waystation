"""Hooks: lifecycle points, the snapshot registry, and the run context (ADR-0008)."""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import get_args

from waystation.errors import StageError
from waystation.results import HookName, HookRaised, Stage
from waystation.sandbox.protocol import Sandbox

logger = logging.getLogger("waystation")

HOOK_NAMES: tuple[HookName, ...] = get_args(HookName)

__all__ = ["HookName", "RunContext"]


@dataclass(slots=True)
class RunState:
    """The mutable facts behind a ``RunContext``; only the orchestrator writes."""

    run_id: str
    name: str | None
    repo: Path
    base_sha: str | None = None
    sandbox: Sandbox | None = None


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

    def __repr__(self) -> str:
        return f"RunContext(run_id={self.run_id!r}, name={self.name!r})"


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
                    logger.error(
                        "run %s: %s hook %s raised after another did: %r",
                        ctx.run_id,
                        hook,
                        _describe(entry.function),
                        exc,
                        exc_info=exc,
                    )
        if first is not None:
            raise first
