"""An exec's stdin: what the process does not read is its own business."""

from __future__ import annotations

from pathlib import Path

import pytest

from helpers import sh
from waystation import NoSandbox, prepare_workspace


@pytest.mark.git
async def test_an_exec_that_exits_without_reading_its_stdin_reports_its_exit(
    host_repo: Path,
) -> None:
    # More than any pipe buffers, so the write is still going when it exits —
    # the way clone_in's bundle meets a script that refuses to clone.
    ws = await prepare_workspace(host_repo)

    async with NoSandbox().start(ws, env={}) as sandbox:
        result = await sandbox.exec(
            [sh(), "-c", "echo refused >&2; exit 3"], stdin="x" * 4_000_000
        )

    assert result.exit_code == 3
    assert result.stderr == "refused\n"
