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
from waystation.errors import StageError
from waystation.observability import GIT, log_argv
from waystation.results import CommandFailed, Stage
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
    repo: Path,
    *args: str,
    stage: Stage,
    check: bool = True,
    env: Mapping[str, str] | None = None,
    stdin: bytes | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ``git -C repo *args``; on failure (with ``check``) raise for ``stage``."""
    argv = ["git", "-C", str(repo), *args]
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
    process = await run_to_end(spawn, waited.append)
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
