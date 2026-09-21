"""Workspace preparation: clone committed refs into a private temp dir."""

from __future__ import annotations

import asyncio
import contextlib
import os
import secrets
import shutil
import stat
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from waystation._git import encode, git_identity, run_git
from waystation.errors import StageError, attributing
from waystation.observability import RUN
from waystation.results import Refused

__all__ = ["Workspace", "prepare_workspace", "remove_workspace"]


@dataclass(frozen=True, slots=True)
class Workspace:
    """A run's private clone at the base ref.

    It carries the branch it is checked out on and the refs that travel with
    it, so neither is re-derived downstream: a transport bundles ``refs``,
    and preservation keeps ``branch`` (#76).
    """

    path: Path
    run_id: str
    base_sha: str
    branch: str
    refs: tuple[str, ...]

    async def remove(self) -> None:
        """Delete the workspace; a failure is logged, never raised (ADR-0016).

        Core's job, not a backend's. The dir is made here and a backend may
        never touch it — a copy transport reads it once and works in its own
        container — so putting the removal on every backend made a leak the
        fault of whichever one forgot (#76).

        It runs on a thread, because it may have to wait: git writes its
        objects read-only, Windows refuses to unlink those, and a process
        that has only just exited can still hold a handle open. Waiting for
        that on the event loop would stall every other run sharing it.
        """
        await asyncio.to_thread(_remove_while_handles_close, self.path)


def _writable_and_retry(
    function: Callable[[str], object], path: str, error: BaseException
) -> None:
    # Git writes objects read-only, and Windows refuses to unlink those.
    if not isinstance(error, PermissionError):
        raise error
    os.chmod(path, stat.S_IWRITE)
    function(path)


def remove_workspace(path: str | os.PathLike[str]) -> None:
    """Delete a workspace dir, read-only git objects included."""
    shutil.rmtree(path, onexc=_writable_and_retry)


# A Windows handle outlives the process that held it by milliseconds, so both
# numbers here span a moment rather than a wedged daemon: five tries, backing
# off 50 ms further each time, is under a second all told. Neither is a knob —
# a caller who wants their own has `remove_workspace` and a loop of their own
# to put it in (ADR-0017).
_REMOVE_ATTEMPTS = 5
_BACKOFF_STEP = 0.05


def _remove_while_handles_close(path: Path) -> None:
    """``remove_workspace``, waiting out an exec that has only just exited.

    Blocking on purpose: ``Workspace.remove`` runs it on a thread.
    """
    last: OSError | None = None
    for attempt in range(_REMOVE_ATTEMPTS):
        try:
            remove_workspace(path)
        except FileNotFoundError:
            return
        except OSError as exc:
            last = exc
            time.sleep(_BACKOFF_STEP * (attempt + 1))
        else:
            return
    RUN.error("could not remove the workspace at %s: %s", path, last)


async def prepare_workspace(
    repo: Path | str,
    *,
    base: str = "HEAD",
    run_id: str | None = None,
    extra_refs: Sequence[str] = (),
) -> Workspace:
    """Resolve ``base``, mint a run id, and clone committed state into a temp dir.

    Each of ``extra_refs`` — a branch or tag name on the host — travels too,
    under the same name, so a resolver run can cherry-pick from a
    preservation branch (ADR-0015). One that names no host ref is refused
    before anything is cloned: ``Refused("missing_extra_ref")``.

    Cancelling it — a workspace bound firing, say — kills the clone's git and
    all it started, and removes the half-made workspace (ADR-0023, #57).

    Raises ``StageError("workspace", ...)``: this is the workspace stage, so
    this is the edge that attributes what host git failed at (ADR-0032).
    """
    host = Path(repo).resolve()
    if not host.exists():
        msg = f"host repo does not exist: {host}"
        raise FileNotFoundError(msg)

    rid = run_id if run_id is not None else secrets.token_hex(4)
    with attributing("workspace"):
        resolved = await run_git(host, "rev-parse", "--verify", base)
        base_sha = resolved.stdout.strip()

        name, email = await git_identity(host)
        extras = await _resolve_extra_refs(host, extra_refs)

        tmp = Path(tempfile.mkdtemp(prefix=f"waystation-{rid}-"))
        branch = f"waystation/{rid}"
        refs = (f"refs/heads/{branch}", *extras)
        try:
            await run_git(
                host, "clone", "--local", "--no-checkout", str(host), str(tmp)
            )
            await run_git(tmp, "checkout", "-B", branch, base_sha)
            if extras:
                # The clone has every object the host has, hardlinked, so a
                # ref is all an extra needs — no fetch (ADR-0037).
                await run_git(
                    tmp,
                    "update-ref",
                    "--stdin",
                    stdin=encode(
                        "".join(f"update {r} {s}\n" for r, s in extras.items())
                    ),
                )
            await _strip_to(tmp, refs)
            await run_git(tmp, "config", "user.name", name)
            await run_git(tmp, "config", "user.email", email)
        except BaseException:
            with contextlib.suppress(OSError):
                remove_workspace(tmp)
            raise

    return Workspace(path=tmp, run_id=rid, base_sha=base_sha, branch=branch, refs=refs)


async def _resolve_extra_refs(host: Path, names: Sequence[str]) -> dict[str, str]:
    """Each extra ref's full name on the host, and what it points at.

    A name must be a ref, not just a revision: ``HEAD~1`` or a sha has no
    name to travel under, so it is refused the way a missing ref is.
    """
    resolved: dict[str, str] = {}
    for name in names:
        shown = await run_git(
            host,
            "rev-parse",
            "--verify",
            "--quiet",
            "--symbolic-full-name",
            name,
            check=False,
        )
        full = shown.stdout.strip()
        if not full.startswith("refs/"):
            detail = (
                f"extra ref {name!r} names no branch or tag in {host}; an "
                "extra ref travels under its own name, so it must be one"
            )
            raise StageError(None, Refused(reason="missing_extra_ref", detail=detail))
        target = await run_git(host, "rev-parse", "--verify", full)
        resolved[full] = target.stdout.strip()
    return resolved


async def _strip_to(workspace: Path, keep: Sequence[str]) -> None:
    """Leave ``keep`` and nothing else: the host's other refs, and ``origin``, go.

    A ``clone --local`` brings every ref the host has — its other branches,
    its tags, and all of them again as ``origin/*`` — while the copy
    transport bundles only the branch that travels. Without this, the same
    flow shows the agent a different repository on a bind backend than in a
    container, and under ``NoSandbox`` an ``origin`` pointing at the host is
    something the agent can push to (#76, ADR-0002).

    Only refs go. The objects stay, hardlinked by the clone and costing
    nothing, which is why this is cheaper than bundling the branch for every
    transport.
    """
    await run_git(workspace, "remote", "remove", "origin")
    listed = await run_git(workspace, "for-each-ref", "--format=%(refname)")
    doomed = [ref for ref in listed.stdout.split() if ref not in keep]
    if doomed:
        # One transaction: a half-stripped workspace is not a state anything
        # downstream should have to recognise (ADR-0027).
        await run_git(
            workspace,
            "update-ref",
            "--stdin",
            stdin=encode("".join(f"delete {ref}\n" for ref in doomed)),
        )
