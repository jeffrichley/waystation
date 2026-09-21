"""DockerSandbox runs the whole loop in a container, over both transports.

The docker tier needs a Linux daemon and the ``waystation-test`` image. With
no daemon it skips (``conftest.py``); with no image it fails, saying how to
build one — missing setup is never mistaken for a pass.
"""

from __future__ import annotations

import asyncio
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
    OK_OUTCOME_LINE,
    PROMPT,
    ShellAgent,
    a_run,
    awaited,
    git,
    git_bytes,
    printf_bytes,
    subjects,
    until,
    workspaces,
)
from waystation import (
    AgentExit,
    CommandFailed,
    DockerSandbox,
    Flow,
    PreflightError,
    Refused,
    RunContext,
    RunFailed,
    RunSucceeded,
    Sandbox,
    ScriptedAgent,
    ScriptedCommit,
    Summary,
    TimedOut,
    Timeouts,
    prepare_workspace,
)
from waystation.agents import AgentLine
from waystation.clock import ManualClock, use_clock
from waystation.sandbox import clone_in
from waystation.sandbox._docker_plans import plan_exec, plan_kill

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
    """The docker tier's image, ready to use — or a failure saying what to do.

    Asked twice when the first answer could not settle it. Docker Desktop is
    seen failing a lookup by name for an image that is listed and readable by
    id, and the listing preflight makes on its way to failing is itself what
    repairs that, so the second ask is answered. What brings the state on is
    not pinned down — which is why this turns on what docker said rather than
    on what kind of host this is. A platform check would encode a guess; this
    is right wherever a lookup comes back unsettled.

    An image docker says is absent is not asked again — it will be absent
    again, and building it is the answer (#97).
    """
    try:
        await DockerSandbox(TEST_IMAGE).preflight()
    except PreflightError as err:
        if isinstance(err.failure, Refused):
            pytest.fail(
                f"{err}\nThe docker tier runs in it: `just test-image` builds it."
            )
        try:
            await DockerSandbox(TEST_IMAGE).preflight()
        except PreflightError as again:
            pytest.fail(
                f"{again}\nAsked twice, so this is not a cold lookup: the image "
                "is not the problem, and docker could not answer for it."
            )
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
async def test_text_that_is_not_utf8_leaves_a_container_as_it_was_committed(
    host_repo: Path, image: str
) -> None:
    # docker exec streams the series raw; keeping its bytes is the host's
    # job, the same one for every backend.
    latin_1 = b"caf\xe9\n\xff\n"
    agent = ShellAgent(
        f"{printf_bytes('latin1.txt', latin_1)} && "
        f"git add -A && git commit -qm latin1 && echo '{OK_OUTCOME_LINE}'",
        shell="sh",
    )

    result = await Flow(host_repo, agent=agent, sandbox=DockerSandbox(image)).run(
        "latin-1"
    )

    assert isinstance(result, RunSucceeded), result
    kept = f"{result.preserved}:latin1.txt"
    assert git_bytes(host_repo, "cat-file", "blob", kept) == latin_1


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
async def test_a_providers_environment_reaches_its_agent_and_nothing_else(
    host_repo: Path, image: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The provider row of ADR-0034's table, on the backend that isolates.

    A provider's credential is for the agent. It must not ride into the
    container's other execs, where `clone_in` and collect run.
    """
    monkeypatch.setenv("WAYSTATION_HOST", "from-host")
    agent_lines: list[str] = []
    other: list[str] = []

    async def keep(ctx: RunContext, line: AgentLine) -> None:
        agent_lines.append(line.raw)

    async def env_outside(ctx: RunContext) -> None:
        other.append((await ctx.sandbox.exec(["env"])).stdout)

    flow = Flow(
        host_repo,
        agent=ShellAgent(
            f"env\necho '{OK_OUTCOME_LINE}'",
            shell="sh",
            env={"WAYSTATION_AGENT": "agent"},
            pass_env=("WAYSTATION_HOST",),
        ),
        sandbox=DockerSandbox(image, env={"WAYSTATION_SPEC": "spec"}),
    )

    result = await (
        flow.run("print the environment", outcome=Summary)
        .on_agent_output(keep)
        .on_sandbox_ready(env_outside)
    )

    assert isinstance(result, RunSucceeded), result
    inside = _as_env("\n".join(agent_lines))
    outside = _as_env(other[0])

    assert inside["WAYSTATION_SPEC"] == "spec"
    assert outside["WAYSTATION_SPEC"] == "spec"
    assert inside["WAYSTATION_AGENT"] == "agent"
    assert inside["WAYSTATION_HOST"] == "from-host"
    assert "WAYSTATION_AGENT" not in outside
    assert "WAYSTATION_HOST" not in outside


def _as_env(text: str) -> dict[str, str]:
    pairs = (line.split("=", 1) for line in text.splitlines() if "=" in line)
    return {key: value for key, value in pairs if key}


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
        async with sandbox.start(ws, env={}):
            started = _docker_calls(caplog)
        stopped = _docker_calls(caplog)[len(started) :]

    assert len(started) == to_start
    assert [call.split()[:3] for call in stopped] == [["docker", "rm", "-f"]]
    assert not ws.path.exists()


@pytest.mark.docker
@pytest.mark.parametrize("transport", TRANSPORTS)
async def test_a_run_in_docker_spends_one_exec_on_its_agent_and_one_on_collect(
    host_repo: Path, image: str, caplog: pytest.LogCaptureFixture, transport: Chosen
) -> None:
    # Collect's checks and its series are one exec, not one each (ADR-0029);
    # a copy spends one more getting the workspace in.
    sandbox = DockerSandbox(image, transport=transport)

    with caplog.at_level(logging.DEBUG, logger="waystation.sandbox"):
        result = await a_run(host_repo, sandbox=sandbox, shell="sh")

    assert isinstance(result, RunSucceeded), result
    called = [call.split()[:2] for call in _docker_calls(caplog)]
    assert called.count(["docker", "exec"]) == (3 if transport == "copy" else 2)


@pytest.mark.docker
async def test_an_exec_streams_stdin_in_and_lines_out_as_they_come(
    host_repo: Path, image: str
) -> None:
    ws = await prepare_workspace(host_repo)
    lines: list[str] = []

    async with DockerSandbox(image).start(ws, env={}) as sandbox:
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

    async with DockerSandbox(image, transport="copy").start(ws, env={}) as sandbox:
        await clone_in(sandbox, ws)
        shown = await sandbox.exec(
            ["sh", "-c", "git rev-parse --abbrev-ref HEAD; git status --porcelain"]
        )

    assert shown.stdout.splitlines() == [f"waystation/{ws.run_id}"]


@pytest.mark.docker
@pytest.mark.parametrize("insistent", [False, True])
async def test_a_cancelled_exec_kills_all_it_started_before_the_cancel_completes(
    host_repo: Path, image: str, insistent: bool
) -> None:
    # Killing the docker client leaves its process running in the container:
    # a lingering child would keep writing into the workspace collect reads
    # (ADR-0023). Cancelled again and again, the kill still runs to its end.
    ws = await prepare_workspace(host_repo)
    started = asyncio.Event()
    linger = "( while :; do printf x >> /tmp/pulse; sleep 0.05; done ) & "

    async with DockerSandbox(image).start(ws, env={}) as sandbox:
        running = asyncio.create_task(
            sandbox.exec(
                ["sh", "-c", linger + "echo started; wait"],
                on_stdout=lambda line: started.set(),
            )
        )
        await until(started.is_set, running)
        running.cancel()
        while insistent and not running.done():
            await asyncio.sleep(0.02)
            running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        pulse = "wc -c < /tmp/pulse; sleep 0.3; wc -c < /tmp/pulse"
        sizes = (await sandbox.exec(["sh", "-c", pulse])).stdout.split()

    assert len(sizes) == 2
    assert sizes[0] == sizes[1]


@pytest.mark.docker
@pytest.mark.parametrize("after", [0.0, 0.1, 0.3, 0.6])
async def test_an_exec_cancelled_as_it_starts_never_runs_on(
    host_repo: Path, image: str, after: float
) -> None:
    # Cancelled at any point of its start — before the client reaches the
    # daemon, or once the process runs but before it has said which group it
    # leads — the exec is stopped, or never begins (ADR-0023).
    ws = await prepare_workspace(host_repo)
    writer = "while :; do printf x >> /tmp/pulse; sleep 0.05; done"

    async with DockerSandbox(image).start(ws, env={}) as sandbox:
        running = asyncio.create_task(sandbox.exec(["sh", "-c", writer]))
        await asyncio.sleep(after)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        # A start the daemon makes late shows itself in the first pause.
        pulse = "sleep 0.5; cat /tmp/pulse | wc -c; sleep 0.3; cat /tmp/pulse | wc -c"
        sizes = await sandbox.exec(["sh", "-c", f"touch /tmp/pulse; {pulse}"])

    first, second = sizes.stdout.split()
    assert first == second


@pytest.mark.docker
def test_an_exec_whose_kill_came_first_never_starts(
    image: str, orphan: Callable[[str], str]
) -> None:
    # The kill can reach the container before the exec has said which group
    # it leads. No timing forces that order through ``exec``, so the plans
    # run here in it, by hand: the exec must see it was killed and not start.
    box = orphan(secrets.token_hex(4))

    subprocess.run(plan_kill(box, "g1"), check=True)
    subprocess.run(
        plan_exec(box, ["touch", "/tmp/ran"], env={}, stdin=False, group="g1"),
        check=False,
    )

    ran = subprocess.run(["docker", "exec", box, "test", "-e", "/tmp/ran"])
    assert ran.returncode != 0


@pytest.mark.docker
@pytest.mark.parametrize("bound", ["agent_silence", "agent_wall"])
async def test_a_bound_in_docker_keeps_the_agents_work_and_stops_its_child(
    host_repo: Path, image: str, bound: Literal["agent_silence", "agent_wall"]
) -> None:
    # As on NoSandbox: the agent commits, then goes quiet with a child still
    # writing. The bound fails the run, the commit is preserved, and the child
    # is gone before collect reads the workspace (ADR-0023).
    clock = ManualClock()
    flow = Flow(
        host_repo,
        agent=ScriptedAgent(
            commits=(ScriptedCommit(message="wip", files={"a.txt": "a"}),),
            linger=True,
            linger_touch="/tmp/pulse",
            shell="sh",
        ),
        sandbox=DockerSandbox(image),
        timeouts=Timeouts(**{bound: 5.0}),
    )
    boxes: list[Sandbox] = []
    sizes: list[str] = []

    async def pulse(ctx: RunContext, exit: AgentExit) -> None:
        twice = "wc -c < /tmp/pulse; sleep 0.3; wc -c < /tmp/pulse"
        sizes.extend((await ctx.sandbox.exec(["sh", "-c", twice])).stdout.split())

    def keep(ctx: RunContext) -> None:
        boxes.append(ctx.sandbox)

    spec = flow.run(PROMPT, outcome=Summary).on_sandbox_ready(keep).on_agent_end(pulse)
    with use_clock(clock):
        task = asyncio.create_task(awaited(spec))
        await until(lambda: bool(boxes and clock._waiters), task)
        # The pulse starts once the commit has landed. Asking the sandbox is
        # an exec, but an awaited one: it never blocks the run it waits on.
        while (await boxes[0].exec(["test", "-e", "/tmp/pulse"])).exit_code:
            if task.done():
                pytest.fail(f"the run ended first: {task.result()!r}")
        clock.advance(5.0)
        result = await task

    assert isinstance(result, RunFailed), result
    assert result.stage == "agent"
    assert isinstance(result.failure, TimedOut)
    assert result.failure.bound == bound
    assert subjects(host_repo, f"HEAD..{result.preserved}") == ["wip"]
    assert len(sizes) == 2
    assert sizes[0] == sizes[1]
    assert _labelled(result.run_id) == []


@pytest.mark.docker
async def test_a_hanging_agent_in_docker_succeeds_and_what_it_left_running_is_killed(
    host_repo: Path, image: str
) -> None:
    # Its Outcome is in, but a child holds stdout open: completion_grace ends
    # the exec, and the child stops before collect reads the workspace.
    flow = Flow(
        host_repo,
        agent=ScriptedAgent(
            outcome=OK_OUTCOME, linger=True, linger_touch="/tmp/pulse", shell="sh"
        ),
        sandbox=DockerSandbox(image),
        timeouts=Timeouts(completion_grace=0.5),
    )
    sizes: list[str] = []

    async def pulse(ctx: RunContext, exit: AgentExit) -> None:
        twice = "wc -c < /tmp/pulse; sleep 0.3; wc -c < /tmp/pulse"
        sizes.extend((await ctx.sandbox.exec(["sh", "-c", twice])).stdout.split())

    result = await flow.run(PROMPT).on_agent_end(pulse)

    assert isinstance(result, RunSucceeded), result
    assert result.agent is not None
    assert result.agent.hanging is True
    assert len(sizes) == 2
    assert sizes[0] == sizes[1]


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
