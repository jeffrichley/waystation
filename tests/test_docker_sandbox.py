"""DockerSandbox runs the whole loop in a container, over both transports.

The docker tier needs a Linux daemon and the ``waystation-test`` image. With
no daemon it skips (``conftest.py``); with no image it fails, saying how to
build one — missing setup is never mistaken for a pass.
"""

from __future__ import annotations

import logging
import secrets
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Literal

import pytest

from helpers import (
    OK_OUTCOME,
    PROMPT,
    a_run,
    git,
    subjects,
    workspaces,
)
from waystation import (
    CommandFailed,
    DockerSandbox,
    Flow,
    PreflightError,
    Refused,
    RunContext,
    RunFailed,
    RunSucceeded,
    ScriptedAgent,
    prepare_workspace,
)
from waystation.sandbox import clone_in

TEST_IMAGE = "waystation-test"
MISSING_IMAGE = "waystation-test-missing:never"

Chosen = Literal["copy", "bind"]

TRANSPORTS = [
    "copy",
    pytest.param(
        "bind",
        marks=pytest.mark.skipif(
            sys.platform == "win32",
            reason="a Windows host binds over 9p as root, so git in the image's "
            "user refuses it as dubious ownership (ADR-0012, ADR-0028); auto "
            "copies there, and the Linux CI job runs bind",
        ),
    ),
]


@pytest.fixture
async def image() -> str:
    """The docker tier's image — or a failure saying how to build it."""
    try:
        await DockerSandbox(TEST_IMAGE).preflight()
    except PreflightError as err:
        pytest.fail(f"{err}\nThe docker tier runs in it: `just test-image` builds it.")
    return TEST_IMAGE


def _labelled(run_id: str) -> list[str]:
    """Containers carrying ``run_id``'s label, running or not, by full id."""
    listed = subprocess.run(
        [
            "docker",
            "ps",
            "-aq",
            "--no-trunc",
            "--filter",
            f"label=waystation.run-id={run_id}",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return listed.stdout.split()


@pytest.fixture
def orphan() -> Iterator[Callable[[str], str]]:
    """Make a sandbox labelled with a run id, as a run killed too hard leaves one.

    Returns its full id. Whatever the test leaves is removed afterwards.
    """
    made: list[str] = []

    def make(run_id: str) -> str:
        started = subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--label",
                f"waystation.run-id={run_id}",
                "--entrypoint",
                "sleep",
                TEST_IMAGE,
                "infinity",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        made.append(started.stdout.strip())
        return made[-1]

    yield make
    if made:
        subprocess.run(["docker", "rm", "-f", *made], check=False, capture_output=True)


def _docker_calls(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Each docker command line the sandbox logged, in order."""
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == "waystation.sandbox" and r.getMessage().startswith("docker ")
    ]


@pytest.mark.docker
@pytest.mark.parametrize("transport", TRANSPORTS)
async def test_a_run_in_docker_lands_its_commits(
    host_repo: Path, isolated_tempdir: Path, image: str, transport: Chosen
) -> None:
    sandbox = DockerSandbox(image, transport=transport)

    result = await a_run(host_repo, sandbox=sandbox, shell="sh").integrate("feature")

    assert isinstance(result, RunSucceeded), result
    assert subjects(host_repo, "HEAD..feature") == ["add a file"]
    assert git(host_repo, "show", "feature:a.txt") == "x"
    assert workspaces(isolated_tempdir) == []


@pytest.mark.docker
@pytest.mark.parametrize("transport", TRANSPORTS)
async def test_work_left_uncommitted_in_docker_is_salvaged(
    host_repo: Path, image: str, transport: Chosen
) -> None:
    flow = Flow(
        host_repo,
        agent=ScriptedAgent(
            outcome=OK_OUTCOME, uncommitted={"notes.txt": "draft"}, shell="sh"
        ),
        sandbox=DockerSandbox(image, transport=transport),
    )

    result = await flow.run(PROMPT)

    assert isinstance(result, RunSucceeded), result
    assert result.series is not None
    assert result.series.salvaged
    assert subjects(host_repo, f"HEAD..{result.preserved}") == [
        "WIP: salvaged uncommitted work"
    ]


@pytest.mark.docker
@pytest.mark.parametrize("transport", TRANSPORTS)
async def test_every_exec_runs_in_the_workspace_root_as_the_images_user(
    host_repo: Path, image: str, transport: Chosen
) -> None:
    seen: list[str] = []

    async def look(ctx: RunContext) -> None:
        shown = await ctx.sandbox.exec(
            ["sh", "-c", "pwd; id -un; git rev-parse --abbrev-ref HEAD"]
        )
        seen.append(shown.stdout)

    sandbox = DockerSandbox(image, transport=transport)
    result = await a_run(host_repo, sandbox=sandbox, shell="sh").on_sandbox_ready(look)

    assert isinstance(result, RunSucceeded), result
    assert seen[0].splitlines() == [
        "/workspace",
        "agent",
        f"waystation/{result.run_id}",
    ]


@pytest.mark.docker
async def test_a_sandbox_sees_only_the_environment_it_names(
    host_repo: Path, image: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WAYSTATION_PASSED", "passed")
    monkeypatch.setenv("WAYSTATION_UNNAMED", "leaked")
    seen: list[str] = []

    async def env_inside(ctx: RunContext) -> None:
        seen.append((await ctx.sandbox.exec(["env"])).stdout)

    sandbox = DockerSandbox(
        image,
        env={"WAYSTATION_LITERAL": "literal"},
        pass_env=("WAYSTATION_PASSED", "WAYSTATION_ABSENT"),
    )
    result = await a_run(host_repo, sandbox=sandbox, shell="sh").on_sandbox_ready(
        env_inside
    )

    assert isinstance(result, RunSucceeded), result
    inside = dict(line.split("=", 1) for line in seen[0].splitlines() if "=" in line)
    assert inside["WAYSTATION_LITERAL"] == "literal"
    assert inside["WAYSTATION_PASSED"] == "passed"
    assert "WAYSTATION_UNNAMED" not in inside
    assert "WAYSTATION_ABSENT" not in inside


@pytest.mark.docker
async def test_a_sandbox_is_labelled_with_its_run_and_gone_after_it(
    host_repo: Path, isolated_tempdir: Path, image: str
) -> None:
    during: list[list[str]] = []

    def labelled(ctx: RunContext) -> None:
        during.append(_labelled(ctx.run_id))

    result = await a_run(
        host_repo, sandbox=DockerSandbox(image), shell="sh"
    ).on_sandbox_ready(labelled)

    assert isinstance(result, RunSucceeded), result
    assert len(during[0]) == 1
    assert _labelled(result.run_id) == []
    assert workspaces(isolated_tempdir) == []


@pytest.mark.docker
@pytest.mark.parametrize(("transport", "to_start"), [("copy", 2), ("bind", 1)])
async def test_a_sandbox_starts_and_stops_in_as_few_docker_calls_as_it_can(
    host_repo: Path,
    image: str,
    caplog: pytest.LogCaptureFixture,
    transport: Chosen,
    to_start: int,
) -> None:
    # One `docker run`, one exec to copy in when copying, one `rm -f`: every
    # call costs ~0.5 s on Docker Desktop, so none is spent that need not be.
    ws = await prepare_workspace(host_repo)
    sandbox = DockerSandbox(image, transport=transport)

    with caplog.at_level(logging.DEBUG, logger="waystation.sandbox"):
        async with sandbox.start(ws, env={}, pass_env=()):
            started = _docker_calls(caplog)
        stopped = _docker_calls(caplog)[len(started) :]

    assert len(started) == to_start
    assert [call.split()[:3] for call in stopped] == [["docker", "rm", "-f"]]
    assert not ws.path.exists()


@pytest.mark.docker
async def test_an_exec_streams_stdin_in_and_lines_out_as_they_come(
    host_repo: Path, image: str
) -> None:
    ws = await prepare_workspace(host_repo)
    lines: list[str] = []

    async with DockerSandbox(image).start(ws, env={}, pass_env=()) as sandbox:
        result = await sandbox.exec(
            ["cat"], stdin="one\ntwo\n", capture=False, on_stdout=lines.append
        )

    assert result.exit_code == 0
    assert lines == ["one", "two"]
    assert result.stdout == "one\ntwo\n"  # capture=False keeps the tail


@pytest.mark.docker
async def test_a_copy_runs_again_over_its_own_work_in_the_container(
    host_repo: Path, image: str
) -> None:
    # A copy that exits 137 is tried again, so it must carry on over what an
    # earlier try left there (ADR-0016).
    ws = await prepare_workspace(host_repo)

    async with DockerSandbox(image, transport="copy").start(
        ws, env={}, pass_env=()
    ) as sandbox:
        await clone_in(sandbox, ws)
        shown = await sandbox.exec(
            ["sh", "-c", "git rev-parse --abbrev-ref HEAD; git status --porcelain"]
        )

    assert shown.stdout.splitlines() == [f"waystation/{ws.run_id}"]


@pytest.mark.docker
async def test_a_start_docker_refuses_fails_the_sandbox_stage_and_leaks_nothing(
    host_repo: Path, isolated_tempdir: Path, image: str
) -> None:
    sandbox = DockerSandbox(image, run_args=("--no-such-flag",))

    result = await a_run(host_repo, sandbox=sandbox, shell="sh")

    assert isinstance(result, RunFailed), result
    assert result.stage == "sandbox"
    assert isinstance(result.failure, CommandFailed)
    assert "--no-such-flag" in result.failure.stderr_tail
    assert _labelled(result.run_id) == []
    assert workspaces(isolated_tempdir) == []


@pytest.mark.docker
async def test_a_missing_image_fails_preflight_with_a_build_hint_and_is_not_pulled(
    host_repo: Path, isolated_tempdir: Path
) -> None:
    with pytest.raises(PreflightError, match="build it first") as raised:
        await DockerSandbox(MISSING_IMAGE).preflight()
    assert raised.value.failure == Refused(
        reason="image_missing", detail=str(raised.value)
    )

    # A lone run preflights itself, so it stops before any stage starts (#30).
    with pytest.raises(PreflightError, match="build it first"):
        await a_run(host_repo, sandbox=DockerSandbox(MISSING_IMAGE), shell="sh")
    assert workspaces(isolated_tempdir) == []

    inspected = subprocess.run(
        ["docker", "image", "inspect", MISSING_IMAGE], check=False, capture_output=True
    )
    assert inspected.returncode != 0  # never pulled


@pytest.mark.docker
async def test_an_image_removed_after_preflight_fails_the_sandbox_stage_as_refused(
    host_repo: Path, isolated_tempdir: Path, image: str
) -> None:
    vanishing = f"waystation-test-vanishing:{secrets.token_hex(4)}"
    subprocess.run(["docker", "tag", image, vanishing], check=True)

    def remove_it(ctx: RunContext) -> None:
        subprocess.run(["docker", "image", "rm", vanishing], check=True)

    try:
        result = await a_run(
            host_repo, sandbox=DockerSandbox(vanishing), shell="sh"
        ).on_workspace_ready(remove_it)
    finally:
        subprocess.run(["docker", "image", "rm", vanishing], check=False)

    assert isinstance(result, RunFailed), result
    assert result.stage == "sandbox"
    assert isinstance(result.failure, Refused)
    assert result.failure.reason == "image_missing"
    assert "build it first" in result.failure.detail
    assert _labelled(result.run_id) == []
    assert workspaces(isolated_tempdir) == []


@pytest.mark.docker
async def test_an_unreachable_daemon_fails_preflight_saying_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DOCKER_HOST", "tcp://127.0.0.1:1")

    with pytest.raises(PreflightError, match="not reachable") as raised:
        await DockerSandbox(TEST_IMAGE).preflight()

    assert isinstance(raised.value.failure, CommandFailed)


@pytest.mark.unit
async def test_no_docker_cli_fails_preflight_saying_to_install_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))

    with pytest.raises(PreflightError, match="install Docker"):
        await DockerSandbox(TEST_IMAGE).preflight()


@pytest.mark.docker
async def test_reap_removes_one_runs_sandboxes_and_says_how_many(
    image: str, orphan: Callable[[str], str]
) -> None:
    run_id, other = secrets.token_hex(4), secrets.token_hex(4)
    orphan(run_id)
    orphan(run_id)
    kept = orphan(other)

    removed = await DockerSandbox.reap(run_id)

    assert removed == 2
    assert _labelled(run_id) == []
    assert _labelled(other) == [kept]


@pytest.mark.docker
async def test_reaping_a_run_with_no_sandboxes_removes_nothing(image: str) -> None:
    assert await DockerSandbox.reap(secrets.token_hex(4)) == 0


@pytest.mark.docker
async def test_nothing_reaps_an_orphan_but_you(
    host_repo: Path, image: str, orphan: Callable[[str], str]
) -> None:
    # Preflight and teardown never reap: the orphan could be a concurrent
    # flow's live sandbox (ADR-0014).
    run_id = secrets.token_hex(4)
    left = orphan(run_id)

    await DockerSandbox(image).preflight()
    result = await a_run(host_repo, sandbox=DockerSandbox(image), shell="sh")

    assert isinstance(result, RunSucceeded), result
    assert _labelled(run_id) == [left]
