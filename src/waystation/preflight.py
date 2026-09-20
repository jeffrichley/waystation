"""Preflight: what runs need from the host, checked before any of them begins.

Fan-out checks its batch whole, and a lone awaited run is a batch of one
(#30, #31). Only ``PreflightError`` leaves a preflight, and nothing is
repaired, installed, built or pulled (ADR-0011, ADR-0016).

Public because preflighting once is a property of a *batch*, not of fan-out:
a scheduler of the user's own — a priority queue, a retry pool, a
``TaskGroup`` — checks its batch here and then runs each spec already
checked, instead of re-checking docker, image and git per run (ADR-0032).
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from waystation._git import require_host_git
from waystation.errors import PreflightError
from waystation.results import Errored

if TYPE_CHECKING:
    from waystation.flow import RunSpec

__all__ = ["preflight"]


@contextlib.contextmanager
def _preflighting(checked: str) -> Iterator[None]:
    """Pass a ``PreflightError`` on; make any other failure one naming ``checked``.

    Only ``PreflightError`` may leave a preflight (#30, ADR-0016): a check
    that breaks some other way is still a reason the run cannot begin.
    """
    try:
        yield
    except PreflightError:
        raise
    except Exception as exc:
        msg = f"{checked} preflight failed: {exc!r}"
        raise PreflightError(msg, failure=Errored(exception=exc)) from exc


def _require_prompt_file(prompt: str | Path) -> None:
    """A prompt given as a path must name a file; a str is the prompt itself."""
    if not isinstance(prompt, Path) or prompt.is_file():
        return
    problem = "is not a file" if prompt.exists() else "not found"
    raise PreflightError(
        f"prompt file {problem}: {prompt} — pass a path to an existing file, "
        "or the prompt itself as a str"
    )


def _distinct[T](values: Iterable[T]) -> list[T]:
    """``values`` without repeats, by equality: a spec holding a mapping has no hash."""
    kept: list[T] = []
    for value in values:
        if value not in kept:
            kept.append(value)
    return kept


async def preflight(specs: Sequence[RunSpec[Any]]) -> None:
    """Check what ``specs`` need from the host, repairing nothing.

    Each distinct agent provider and sandbox spec is checked once — equal
    values describe the same thing, so a batch of fifty runs on one image
    checks it once — then every prompt file, then the host's git. Any problem
    raises ``PreflightError`` naming what failed and how to fix it; nothing is
    installed, built or pulled (#30, #31, ADR-0011).

    A spec whose batch was checked here runs through
    ``await spec.perform(preflighted=True)``, which skips the check it has
    already had.

    Args:
        specs: The batch to check. Repeats are fine: equal agent providers
            and sandbox specs describe the same thing and are checked once.

    Raises:
        PreflightError: Naming what failed and how to fix it. No run has
            started.
    """
    for agent in _distinct(spec.provider for spec in specs):
        with _preflighting(type(agent).__name__):
            agent.preflight()
    for sandbox in _distinct(spec.backend for spec in specs):
        with _preflighting(type(sandbox).__name__):
            await sandbox.preflight()
    for spec in specs:
        with _preflighting("prompt file"):
            _require_prompt_file(spec.prompt)
    with _preflighting("host git"):
        await require_host_git()
