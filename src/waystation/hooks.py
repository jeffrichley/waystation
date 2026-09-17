"""Hooks: lifecycle points, the snapshot registry, and the run context (ADR-0008)."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, get_args

from waystation.errors import StageError
from waystation.results import HookRaised, Stage
from waystation.sandbox.protocol import Sandbox

HookName = Literal[
    "run_start",
    "workspace_ready",
    "sandbox_ready",
    "agent_output",
    "agent_end",
    "integrated",
    "run_end",
]

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
        return self._state.run_id

    @property
    def name(self) -> str | None:
        return self._state.name

    @property
    def repo(self) -> Path:
        return self._state.repo

    @property
    def base_sha(self) -> str | None:
        return self._state.base_sha

    @property
    def sandbox(self) -> Sandbox:
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
    ) -> None:
        """Call each function at ``hook`` in order, awaiting async ones.

        A raising function stops the rest and raises
        ``StageError(stage, HookRaised(...))``.
        """
        for entry in self.entries:
            if entry.hook != hook:
                continue
            try:
                returned = entry.function(ctx, *args)
                if inspect.isawaitable(returned):
                    await returned
            except Exception as exc:
                raise StageError(
                    stage,
                    HookRaised(
                        hook=hook,
                        function=_describe(entry.function),
                        exception=exc,
                    ),
                ) from exc
