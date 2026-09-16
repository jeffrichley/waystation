"""Workspace preparation: clone committed refs into a private temp dir."""

from __future__ import annotations

import secrets
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Workspace:
    """A run's private clone at the base ref."""

    path: Path
    run_id: str
    base_sha: str
    host_repo: Path


def _git(
    repo: Path, *args: str, check: bool = True
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=check,
        capture_output=True,
        text=True,
    )


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
        msg = "host repo has no git identity (user.name / user.email)"
        raise RuntimeError(msg)

    tmp = Path(tempfile.mkdtemp(prefix=f"waystation-{rid}-"))
    try:
        subprocess.run(
            ["git", "clone", "--local", "--no-checkout", str(host), str(tmp)],
            check=True,
            capture_output=True,
            text=True,
        )
        branch = f"waystation/{rid}"
        _git(tmp, "checkout", "-B", branch, base_sha)
        _git(tmp, "config", "user.name", name.stdout.strip())
        _git(tmp, "config", "user.email", email.stdout.strip())
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise

    return Workspace(path=tmp, run_id=rid, base_sha=base_sha, host_repo=host)
