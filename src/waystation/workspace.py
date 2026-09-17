"""Workspace preparation: clone committed refs into a private temp dir."""

from __future__ import annotations

import contextlib
import os
import secrets
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from waystation.errors import StageError
from waystation.observability import GIT, log_argv
from waystation.results import CommandFailed, Refused
from waystation.tails import bound_tail


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


def _git(
    repo: Path, *args: str, check: bool = True
) -> subprocess.CompletedProcess[str]:
    argv = ["git", "-C", str(repo), *args]
    log_argv(GIT, argv)
    result = subprocess.run(
        argv,
        check=False,
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        raise StageError(
            "workspace",
            CommandFailed(
                argv=tuple(argv),
                exit_code=result.returncode,
                stderr_tail=bound_tail(result.stderr),
            ),
        )
    return result


def prepare_workspace(
    repo: Path | str,
    *,
    base: str = "HEAD",
    run_id: str | None = None,
) -> Workspace:
    """Resolve ``base``, mint a run id, and clone committed state into a temp dir."""
    host = Path(repo).resolve()
    if not host.exists():
        msg = f"host repo does not exist: {host}"
        raise FileNotFoundError(msg)

    rid = run_id if run_id is not None else secrets.token_hex(4)
    base_sha = _git(host, "rev-parse", "--verify", base).stdout.strip()

    name = _git(host, "config", "--get", "user.name", check=False)
    email = _git(host, "config", "--get", "user.email", check=False)
    if (
        name.returncode != 0
        or email.returncode != 0
        or not name.stdout.strip()
        or not email.stdout.strip()
    ):
        raise StageError(
            "workspace",
            Refused(
                reason="no_git_identity",
                detail="host repo has no git identity (user.name / user.email)",
            ),
        )

    tmp = Path(tempfile.mkdtemp(prefix=f"waystation-{rid}-"))
    try:
        clone_argv = [
            "git",
            "clone",
            "--local",
            "--no-checkout",
            str(host),
            str(tmp),
        ]
        log_argv(GIT, clone_argv)
        clone = subprocess.run(
            clone_argv,
            check=False,
            capture_output=True,
            text=True,
        )
        if clone.returncode != 0:
            raise StageError(
                "workspace",
                CommandFailed(
                    argv=tuple(clone_argv),
                    exit_code=clone.returncode,
                    stderr_tail=bound_tail(clone.stderr),
                ),
            )
        branch = f"waystation/{rid}"
        _git(tmp, "checkout", "-B", branch, base_sha)
        _git(tmp, "config", "user.name", name.stdout.strip())
        _git(tmp, "config", "user.email", email.stdout.strip())
    except Exception:
        with contextlib.suppress(OSError):
            remove_workspace(tmp)
        raise

    return Workspace(path=tmp, run_id=rid, base_sha=base_sha, host_repo=host)
