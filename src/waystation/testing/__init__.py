"""Test support waystation ships: a token-free agent, and a backend's contract.

``ScriptedAgent`` plays back canned output, Outcomes and commits, so a flow
script can be tested without spending tokens. ``SandboxConformance`` is the
sandbox contract as pytest tests, for people writing their own backends.

Only the suite needs pytest, which waystation does not otherwise depend on:
install it as ``waystation[testing]``. It is resolved on first touch, so
importing ``ScriptedAgent`` from here never needs pytest (ADR-0044).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from waystation.testing.scripted import ScriptedAgent, ScriptedCommit

if TYPE_CHECKING:
    from waystation.testing.conformance import SandboxConformance

__all__ = ["SandboxConformance", "ScriptedAgent", "ScriptedCommit"]


def __getattr__(name: str) -> Any:
    # The suite imports pytest at module level, so it is loaded only when asked
    # for: a ScriptedAgent user must not need waystation[testing] (ADR-0044).
    if name == "SandboxConformance":
        from waystation.testing.conformance import SandboxConformance

        return SandboxConformance
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
