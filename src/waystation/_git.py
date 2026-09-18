"""Host-side git runner shared by the stages that touch the host repo.

Each call is a subprocess owned by the task awaiting it. Cancel that task — a
stage's bound firing, say — and git dies with everything it started before the
cancellation completes (ADR-0023): no git outlives the stage that ran it,
which a git run in a thread would (#57).
"""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path

from waystation._cancellation import run_to_end
from waystation.errors import PreflightError, StageError
from waystation.observability import GIT, log_argv
from waystation.results import CommandFailed, Errored, Stage
from waystation.sandbox.processes import host_processes
from waystation.tails import bound_tail


def decode(output: bytes) -> str:
    """Bytes from git as text, without newline translation.

    Never use text-mode pipes for git: on Windows they rewrite "\\n" as "\\r\\n"
    (and back), which changes every line of a patch. surrogateescape keeps
    bytes that are not UTF-8.
    """
    return output.decode("utf-8", errors="surrogateescape")


def encode(text: str) -> bytes:
    """Text for git as bytes; the inverse of ``decode``."""
    return text.encode("utf-8", errors="surrogateescape")


async def run_git(
    repo: Path | None,
    *args: str,
    stage: Stage,
    check: bool = True,
    env: Mapping[str, str] | None = None,
    stdin: bytes | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ``git -C repo *args``; on failure (with ``check``) raise for ``stage``.

    ``repo=None`` runs git where the process is, for what needs no repo.
    """
    argv = ["git", *(("-C", str(repo)) if repo is not None else ()), *args]
    log_argv(GIT, argv)
    processes = host_processes()
    # A cancellation during the spawn waits for the tree to be adopted, so it
    # kills the whole tree instead of the one process asyncio would.
    waited: list[asyncio.CancelledError] = []
    spawn = asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL if stdin is None else asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=dict(env) if env is not None else None,
        **processes.spawn_options(),
    )
    try:
        process = await run_to_end(spawn, waited.append)
    except Exception:
        if waited:  # a cancellation that waited outranks a spawn that failed
            raise waited[0] from None
        raise
    tree = processes.adopt(process)
    try:
        if waited:
            raise waited[0]
        stdout, stderr = await process.communicate(stdin)
    except asyncio.CancelledError:
        tree.kill()
        with suppress(asyncio.CancelledError):
            await process.wait()
        raise
    finally:
        tree.release()
    assert process.returncode is not None
    result = subprocess.CompletedProcess(
        argv, process.returncode, decode(stdout), decode(stderr)
    )
    if check and result.returncode != 0:
        raise StageError(
            stage,
            CommandFailed(
                argv=tuple(argv),
                exit_code=result.returncode,
                stderr_tail=bound_tail(result.stderr),
            ),
        )
    return result


# Landing replays each commit with `git merge-tree --merge-base`, which older
# git lacks: the one host-version floor waystation has (ADR-0020).
_OLDEST_GIT = (2, 40)


async def require_host_git() -> None:
    """Raise ``PreflightError``, saying how to fix it, unless host git is new enough."""
    wanted = ".".join(map(str, _OLDEST_GIT))
    try:
        # check=False: no stage owns preflight, so nothing here raises a
        # StageError; the stage named is only the runner's required label.
        shown = await run_git(None, "--version", stage="workspace", check=False)
    except FileNotFoundError as exc:
        msg = f"git is not on PATH: install git {wanted} or newer"
        raise PreflightError(msg, failure=Errored(exc)) from exc
    except OSError as exc:
        msg = f"could not run git ({exc}): check the git install on PATH"
        raise PreflightError(msg, failure=Errored(exc)) from exc
    if shown.returncode != 0:
        raise PreflightError(
            f"`git --version` exited {shown.returncode}: "
            f"{bound_tail(shown.stderr).strip()} — check the git install on PATH"
        )
    raw = shown.stdout.strip()  # e.g. "git version 2.43.0.windows.1"
    version = raw.removeprefix("git version ")
    try:
        found = tuple(int(part) for part in version.split(".")[:2])
    except ValueError as exc:
        msg = f"could not read a git version from {raw!r}: is `git` on PATH git?"
        raise PreflightError(msg, failure=Errored(exc)) from exc
    if found < _OLDEST_GIT:
        raise PreflightError(
            f"host git is {version}, but waystation needs {wanted} or newer: "
            f"upgrade git (landing uses `git merge-tree --merge-base`)"
        )
