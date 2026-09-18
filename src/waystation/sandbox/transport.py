"""Transport: how a workspace gets into a sandbox (ADR-0012).

Bind is each backend's own business — a mount, or nothing at all. Copy works
on any backend through the protocol's exec, so it lives here once, as a helper
a copying backend calls, not as a seam it implements (ADR-0028).
"""

from __future__ import annotations

import base64
import shlex
from pathlib import Path

from waystation._git import encode, run_git
from waystation.errors import StageError
from waystation.results import CommandFailed
from waystation.sandbox.protocol import Sandbox
from waystation.tails import bound_tail
from waystation.workspace import Workspace

__all__ = ["clone_in"]


async def clone_in(sandbox: Sandbox, ws: Workspace) -> None:
    """Copy ``ws`` into ``sandbox``, cloned there as the sandbox's own user.

    The workspace branch leaves the host as a git bundle and arrives over one
    exec's stdin, base64 because exec stdin is text. The clone lands in the
    sandbox's workspace root — an empty directory its user can write — with
    the branch checked out and the host's git identity set. Only commits
    travel: anything uncommitted in ``ws`` stays behind.

    Raises ``StageError("sandbox", CommandFailed)`` when the clone fails.
    """
    branch = f"waystation/{ws.run_id}"
    bundle = await run_git(
        ws.path, "bundle", "create", "-", f"refs/heads/{branch}", stage="sandbox"
    )
    script = _clone_script(
        branch,
        name=await _identity(ws.path, "user.name"),
        email=await _identity(ws.path, "user.email"),
    )
    argv = ("sh", "-c", script)
    result = await sandbox.exec(
        argv, stdin=base64.b64encode(encode(bundle.stdout)).decode("ascii")
    )
    if result.exit_code != 0:
        raise StageError(
            "sandbox",
            CommandFailed(
                argv=argv,
                exit_code=result.exit_code,
                stderr_tail=bound_tail(result.stderr),
            ),
        )


def _clone_script(branch: str, *, name: str, email: str) -> str:
    """One exec does the whole clone: every docker call costs ~0.5 s on Windows."""
    lines = [
        "set -e",
        '[ -w . ] || { echo "waystation: $(pwd) is not writable by $(id -un)," '
        '"so the workspace cannot be cloned into it" >&2; exit 1; }',
        'bundle="$(mktemp)"',
        "trap 'rm -f \"$bundle\"' EXIT",
        'base64 -d > "$bundle"',
        f'git clone --quiet --branch {shlex.quote(branch)} "$bundle" .',
        # The bundle is gone once this exec ends; a remote naming it would lie.
        "git remote remove origin",
    ]
    if name:
        lines.append(f"git config user.name {shlex.quote(name)}")
    if email:
        lines.append(f"git config user.email {shlex.quote(email)}")
    return "\n".join(lines)


async def _identity(workspace: Path, key: str) -> str:
    """The workspace's ``key`` from git config, or ``""`` when it has none."""
    shown = await run_git(
        workspace, "config", "--get", key, stage="sandbox", check=False
    )
    return shown.stdout.strip() if shown.returncode == 0 else ""
