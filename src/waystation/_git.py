"""Host-side git runner shared by the stages that touch the host repo."""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from pathlib import Path

from waystation.errors import StageError
from waystation.results import CommandFailed, Stage
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


def run_git(
    repo: Path,
    *args: str,
    stage: Stage,
    check: bool = True,
    env: Mapping[str, str] | None = None,
    stdin: bytes | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ``git -C repo *args``; on failure (with ``check``) raise for ``stage``."""
    argv = ["git", "-C", str(repo), *args]
    raw = subprocess.run(
        argv,
        check=False,
        capture_output=True,
        env=dict(env) if env is not None else None,
        input=stdin,
    )
    result = subprocess.CompletedProcess(
        argv, raw.returncode, decode(raw.stdout), decode(raw.stderr)
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
