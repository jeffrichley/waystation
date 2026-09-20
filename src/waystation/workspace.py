"""Workspace preparation: clone committed refs into a private temp dir."""

from __future__ import annotations

import contextlib
import os
import secrets
import shutil
import stat
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from waystation._git import git_identity, run_git


@dataclass(frozen=True, slots=True)
class Workspace:
    """A run's private clone at the base ref."""

    path: Path
    run_id: str
    base_sha: str
    host_repo: Path


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
    """
    host = Path(repo).resolve()
    if not host.exists():
        msg = f"host repo does not exist: {host}"
        raise FileNotFoundError(msg)

    rid = run_id if run_id is not None else secrets.token_hex(4)
    resolved = await run_git(host, "rev-parse", "--verify", base, stage="workspace")
    base_sha = resolved.stdout.strip()

    name, email = await git_identity(host, stage="workspace")

    tmp = Path(tempfile.mkdtemp(prefix=f"waystation-{rid}-"))
    try:
        await run_git(
            host,
            "clone",
            "--local",
            "--no-checkout",
            str(host),
            str(tmp),
            stage="workspace",
        )
        branch = f"waystation/{rid}"
        await run_git(tmp, "checkout", "-B", branch, base_sha, stage="workspace")
        await run_git(tmp, "config", "user.name", name, stage="workspace")
        await run_git(tmp, "config", "user.email", email, stage="workspace")
    except BaseException:
        with contextlib.suppress(OSError):
            remove_workspace(tmp)
        raise

    return Workspace(path=tmp, run_id=rid, base_sha=base_sha, host_repo=host)
