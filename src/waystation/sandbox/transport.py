"""Transport: how a workspace gets into a sandbox (ADR-0012).

Bind is each backend's own business — a mount, or nothing at all. Copy works
on any backend through the protocol's exec, so it lives here once, as a helper
a copying backend calls, not as a seam it implements (ADR-0028).
"""

from __future__ import annotations

import base64
import shlex
from collections.abc import Sequence

from waystation._git import config_value, encode, run_git
from waystation.clock import get_clock
from waystation.errors import StageError, attributing
from waystation.observability import SANDBOX
from waystation.results import CommandFailed
from waystation.sandbox.protocol import ExecResult, Sandbox
from waystation.tails import bound_tail
from waystation.workspace import Workspace

__all__ = ["clone_in"]

# The one retry anywhere, and its numbers are #18's, with no setting: a
# sandbox exec whose shell could not start (126) or was SIGKILLed as it
# started (137) is a race of the sandbox's own that the next try is past
# (ADR-0016). Sandcastle retries the same codes the same way.
_TRANSIENT = frozenset({126, 137})
_RETRIES = 2
_RETRY_DELAY = 0.25

# Left in the clone's .git: this directory holds a copy, or a try at one.
_MARKER = ".git/waystation-clone"


async def clone_in(sandbox: Sandbox, ws: Workspace) -> None:
    """Copy ``ws`` into ``sandbox``, cloned there as the sandbox's own user.

    The workspace branch leaves the host as a git bundle and arrives over one
    exec's stdin, base64 because exec stdin is text. The clone lands in the
    sandbox's workspace root — an empty directory its user can write — with
    the branch checked out and the host's git identity set. Only commits
    travel: anything uncommitted in ``ws`` stays behind.

    The exec is idempotent, so one that exits 126 or 137 is tried again,
    twice at most, 250 ms apart (ADR-0016).

    Raises ``StageError("sandbox", CommandFailed)`` when the clone fails.
    """
    branch = f"waystation/{ws.run_id}"
    with attributing("sandbox"):
        bundle = await run_git(ws.path, "bundle", "create", "-", f"refs/heads/{branch}")
        script = _clone_script(
            branch,
            name=await config_value(ws.path, "user.name"),
            email=await config_value(ws.path, "user.email"),
        )
    # The sandbox's own shell, not a spelling of one: a host path means
    # nothing in a container, and a bare `sh` is not on a Windows host's
    # PATH (ADR-0036).
    argv = (*sandbox.shell, script)
    result = await _setup_exec(
        sandbox, argv, stdin=base64.b64encode(encode(bundle.stdout)).decode("ascii")
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


async def _setup_exec(
    sandbox: Sandbox, argv: Sequence[str], *, stdin: str
) -> ExecResult:
    """Exec ``argv``, which must be idempotent: tried again on a transient exit."""
    for tried in range(1, _RETRIES + 1):
        result = await sandbox.exec(argv, stdin=stdin)
        if result.exit_code not in _TRANSIENT:
            return result
        SANDBOX.warning(
            "setup exec exited %d; trying again (%d of %d)",
            result.exit_code,
            tried,
            _RETRIES,
        )
        await get_clock().sleep(_RETRY_DELAY)
    return await sandbox.exec(argv, stdin=stdin)


def _clone_script(branch: str, *, name: str, email: str) -> str:
    """One exec does the whole clone: an exec can be a costly round trip.

    A ``docker exec`` is about 0.5 s on Docker Desktop, for one.

    It fetches into a repository it makes rather than ``git clone``, which
    refuses a directory an earlier try has written to: a try killed part way
    is tried again, and carries on over what it left. The marker in ``.git``
    says that is what the directory holds, so anything else is still refused.
    """
    ref = f"refs/heads/{branch}"
    lines = [
        "set -e",
        '[ -w . ] || { echo "waystation: $(pwd) is not writable by $(id -un)," '
        '"so the workspace cannot be cloned into it" >&2; exit 1; }',
        f"if [ ! -e {_MARKER} ]; then",
        '  [ -z "$(ls -A)" ] || { echo "waystation: $(pwd) is not an empty '
        'directory," "so the workspace cannot be cloned into it" >&2; exit 1; }',
        f"  mkdir .git && : > {_MARKER}",
        "fi",
        'bundle="$(mktemp)"',
        "trap 'rm -f \"$bundle\"' EXIT",
        'base64 -d > "$bundle"',
        "git init --quiet",
        # --update-head-ok: a second try's HEAD is already on the branch.
        f'git fetch --quiet --update-head-ok "$bundle" {shlex.quote(f"+{ref}:{ref}")}',
        f"git symbolic-ref HEAD {shlex.quote(ref)}",
        "git reset --quiet --hard",
    ]
    if name:
        lines.append(f"git config user.name {shlex.quote(name)}")
    if email:
        lines.append(f"git config user.email {shlex.quote(email)}")
    return "\n".join(lines)
