"""Workspace preparation: clone committed refs into a private temp dir."""

from __future__ import annotations

import contextlib
import os
import secrets
import shutil
import stat
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from waystation._git import encode, git_identity, run_git
from waystation.errors import attributing

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


async def prepare_workspace(
    repo: Path | str,
    *,
    base: str = "HEAD",
    run_id: str | None = None,
) -> Workspace:
    """Resolve ``base``, mint a run id, and clone committed state into a temp dir.

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

        tmp = Path(tempfile.mkdtemp(prefix=f"waystation-{rid}-"))
        branch = f"waystation/{rid}"
        refs = (f"refs/heads/{branch}",)
        try:
            await run_git(
                host, "clone", "--local", "--no-checkout", str(host), str(tmp)
            )
            await run_git(tmp, "checkout", "-B", branch, base_sha)
            await _strip_to(tmp, refs)
            await run_git(tmp, "config", "user.name", name)
            await run_git(tmp, "config", "user.email", email)
        except BaseException:
            with contextlib.suppress(OSError):
                remove_workspace(tmp)
            raise

    return Workspace(
        path=tmp, run_id=rid, base_sha=base_sha, branch=branch, refs=refs
    )


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
