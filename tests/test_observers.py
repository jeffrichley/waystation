"""RunLogFiles and EventLog hook bundles (issue #34)."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from helpers import (
    OK_OUTCOME,
    OK_OUTCOME_LINE,
    PROMPT,
    WORKS_UNTIL_STOPPED,
    ShellAgent,
    a_run,
)
from waystation import (
    Errored,
    EventLog,
    Flow,
    NoSandbox,
    RunFailed,
    RunLogFiles,
    RunSpec,
    RunSucceeded,
    ScriptedAgent,
    Summary,
    configure_logging,
    fan_out,
    observers,
)
from waystation.agents import AgentLine
from waystation.hooks import RunContext, RunState


@pytest.mark.git
async def test_ctx_carries_the_prompt_from_run_start(host_repo: Path) -> None:
    seen: list[str] = []

    result = await a_run(host_repo).on_run_start(lambda ctx: seen.append(ctx.prompt))

    assert isinstance(result, RunSucceeded)
    assert seen == [PROMPT]


@pytest.mark.git
async def test_a_prompt_held_in_a_file_reaches_ctx_as_text(
    host_repo: Path, tmp_path: Path
) -> None:
    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text("Read the file.\n", encoding="utf-8")
    seen: list[str] = []

    result = await a_run(host_repo, prompt_file).on_run_start(
        lambda ctx: seen.append(ctx.prompt)
    )

    assert isinstance(result, RunSucceeded)
    assert seen == ["Read the file.\n"]


@pytest.mark.git
async def test_run_log_files_writes_one_tailable_file_per_run(
    host_repo: Path, tmp_path: Path
) -> None:
    logs = tmp_path / "logs"

    result = await a_run(host_repo).hooks(RunLogFiles(logs))

    assert isinstance(result, RunSucceeded)
    text = (logs / f"{result.run_id}.log").read_text(encoding="utf-8")
    assert PROMPT in text, "the full prompt belongs in the run file"
    assert "working" in text, "raw agent stdout belongs in the run file"
    assert "workspace ready" in text, "lifecycle lines interleave with output"
    assert "--- exit code: 0 ---" in text, "a footer closes the file"


@pytest.mark.git
async def test_stderr_lines_are_marked_and_stdout_is_raw(
    host_repo: Path, tmp_path: Path
) -> None:
    logs = tmp_path / "logs"
    flow = Flow(
        host_repo,
        agent=ShellAgent(
            f"echo working; echo trouble >&2; echo '{OK_OUTCOME_LINE}'",
        ),
        sandbox=NoSandbox(),
        hooks=[RunLogFiles(logs)],
    )

    result = await flow.run("say something")

    assert isinstance(result, RunSucceeded)
    text = (logs / f"{result.run_id}.log").read_text(encoding="utf-8")
    assert "\nworking\n" in text, "stdout is written raw"
    assert "[stderr] trouble" in text, "stderr is marked as such"


@pytest.mark.git
async def test_a_hooks_own_lines_interleave_with_the_agents(
    host_repo: Path, tmp_path: Path
) -> None:
    logs = tmp_path / "logs"

    result = await (
        a_run(host_repo)
        .hooks(RunLogFiles(logs))
        .on_sandbox_ready(lambda ctx: ctx.log.info("setup finished"))
    )

    assert isinstance(result, RunSucceeded)
    lines = (logs / f"{result.run_id}.log").read_text(encoding="utf-8").splitlines()
    hook_line = next(i for i, line in enumerate(lines) if "setup finished" in line)
    agent_line = next(i for i, line in enumerate(lines) if line == "working")
    assert hook_line < agent_line, "the hook ran before the agent spoke"


@pytest.mark.unit
def test_a_named_run_writes_a_file_named_for_it(tmp_path: Path) -> None:
    """Until #29 lands ``RunSpec.name()`` no run can be named, so drive it here."""
    state = RunState(run_id="0badcafe", name="nightly", repo=tmp_path, prompt="hi")
    logs = tmp_path / "logs"
    bundle = RunLogFiles(logs)
    ctx = RunContext(state)

    bundle.on_run_start(ctx)
    try:
        assert (logs / "nightly-0badcafe.log").exists()
    finally:
        bundle.on_run_end(ctx, _unfinished(state))


def _unfinished(state: RunState) -> RunFailed:
    """The shape of a result, only so a test can close the file it opened."""
    return RunFailed(
        run_id=state.run_id,
        name=state.name,
        base_sha=None,
        elapsed={},
        agent=None,
        series=None,
        preserved=None,
        stage="agent",
        failure=Errored(exception=RuntimeError("test")),
    )


@pytest.mark.git
async def test_the_run_file_is_readable_while_the_run_is_still_going(
    host_repo: Path, tmp_path: Path
) -> None:
    """``tail -f`` is the point: a line must be on disk before the run ends."""
    logs = tmp_path / "logs"
    midrun: list[str] = []

    def peek(ctx: RunContext, line: AgentLine) -> None:
        midrun.append((logs / f"{ctx.run_id}.log").read_text(encoding="utf-8"))

    flow = Flow(
        host_repo,
        agent=ShellAgent(f"echo working; echo more; echo '{OK_OUTCOME_LINE}'"),
        sandbox=NoSandbox(),
        hooks=[RunLogFiles(logs)],
    )
    result = await flow.run("say something").on_agent_output(peek)

    assert isinstance(result, RunSucceeded)
    assert any("workspace ready" in text for text in midrun), (
        "lifecycle lines reach disk as they happen, not at close"
    )
    assert any("working" in text for text in midrun), (
        "an agent line is flushed before the next one arrives"
    )
    assert not any("exit code" in text for text in midrun), (
        "the footer is only written once the output has drained"
    )


def _attached() -> tuple[list[logging.Handler], int]:
    """What a run file attaches to the ``waystation`` logger, and holds it at."""
    root = logging.getLogger("waystation")
    return list(root.handlers), root.level


@pytest.mark.git
async def test_leaving_a_fan_out_early_finishes_every_run_file(
    host_repo: Path, tmp_path: Path
) -> None:
    """A cancelled run fires no run_end (ADR-0017), yet its file still ends."""
    logs = tmp_path / "logs"
    before = _attached()
    working: set[str] = set()
    both = asyncio.Event()

    def watch(ctx: RunContext, line: AgentLine) -> None:
        if line.raw == "ready":
            working.add(ctx.run_id)
            if len(working) == 2:
                both.set()

    spec: RunSpec[Summary] = (
        Flow(
            host_repo,
            agent=ShellAgent(WORKS_UNTIL_STOPPED),
            sandbox=NoSandbox(),
            hooks=[RunLogFiles(logs)],
        )
        .run("work until stopped")
        .on_agent_output(watch)
    )
    async with fan_out([spec, spec]):
        await both.wait()

    for run_id in working:
        lines = (logs / f"{run_id}.log").read_text(encoding="utf-8").splitlines()
        assert any("run cancelled during agent" in line for line in lines)
        assert lines[-1] == "--- no result ---"
    assert _attached() == before, "no handler left attached, no level left held"


@pytest.mark.git
async def test_the_block_finishes_a_run_file_whose_task_outlived_the_run(
    host_repo: Path, tmp_path: Path
) -> None:
    logs = tmp_path / "logs"
    before = _attached()

    def cancel_this_run(ctx: RunContext) -> None:
        task = asyncio.current_task()
        assert task is not None
        task.cancel()

    with RunLogFiles(logs) as files:
        with pytest.raises(asyncio.CancelledError):
            await a_run(host_repo).hooks(files).on_run_start(cancel_this_run)
        (log,) = logs.glob("*.log")
        # This task goes on, so nothing has ended the file yet.
        assert "--- no result ---" not in log.read_text(encoding="utf-8")

    assert log.read_text(encoding="utf-8").splitlines()[-1] == "--- no result ---"
    assert _attached() == before


def events_in(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.mark.git
async def test_event_log_writes_one_object_per_lifecycle_event(
    host_repo: Path, tmp_path: Path
) -> None:
    path = tmp_path / "events.jsonl"

    result = await a_run(host_repo).hooks(EventLog(path)).integrate("landing")

    assert isinstance(result, RunSucceeded)
    events = events_in(path)
    assert [event["event"] for event in events] == [
        "run_start",
        "workspace_ready",
        "sandbox_ready",
        "agent_end",
        "integrated",
        "run_end",
    ]
    assert {event["run_id"] for event in events} == {result.run_id}
    assert all(event["ts"] and "name" in event for event in events)
    by_event = {event["event"]: event for event in events}
    assert by_event["agent_end"]["exit_code"] == 0, (
        "the event's typed fields ride along"
    )
    assert by_event["integrated"]["target"] == "landing"
    assert by_event["workspace_ready"]["base_sha"] == result.base_sha


@pytest.mark.git
async def test_event_log_carries_agent_output_only_when_asked(
    host_repo: Path, tmp_path: Path
) -> None:
    quiet, loud = tmp_path / "quiet.jsonl", tmp_path / "loud.jsonl"

    await a_run(host_repo).hooks(EventLog(quiet))
    await a_run(host_repo).hooks(EventLog(loud, include_output=True))

    assert not [e for e in events_in(quiet) if e["event"] == "agent_output"]
    spoken = [e for e in events_in(loud) if e["event"] == "agent_output"]
    assert [e["raw"] for e in spoken][0] == "working"
    assert {e["stream"] for e in spoken} == {"stdout"}


@pytest.mark.git
async def test_both_bundles_register_like_a_users_own(
    host_repo: Path, tmp_path: Path
) -> None:
    """AC3: the flow constructor for one, the per-run chain for the other."""
    logs, events = tmp_path / "logs", tmp_path / "events.jsonl"
    flow = Flow(
        host_repo,
        agent=ScriptedAgent(lines=["working"], outcome=OK_OUTCOME),
        sandbox=NoSandbox(),
        hooks=[EventLog(events)],
    )

    result = await flow.run("say something").hooks(RunLogFiles(logs))

    assert isinstance(result, RunSucceeded)
    assert (logs / f"{result.run_id}.log").exists()
    assert [e["event"] for e in events_in(events)][0] == "run_start"


@pytest.mark.git
async def test_a_broken_observer_logs_at_error_and_leaves_the_run_alone(
    host_repo: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A full disk costs a log line, never the agent's commits (ADR-0001)."""
    blocked = tmp_path / "logs"
    blocked.write_text("a file where the directory should be", encoding="utf-8")

    with caplog.at_level(logging.ERROR, logger="waystation"):
        result = await a_run(host_repo).hooks(
            RunLogFiles(blocked), EventLog(blocked / "events.jsonl")
        )

    assert isinstance(result, RunSucceeded), "observability never fails the work"
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert any("RunLogFiles" in message for message in errors)
    assert any("EventLog" in message for message in errors)


# RunLog is the one left out: the orchestrator calls it directly, it is never
# registered, so it cannot fail a run as a hook.
SHIPPED = [getattr(observers, name) for name in observers.__all__ if name != "RunLog"]


@pytest.mark.unit
@pytest.mark.parametrize("observer", SHIPPED, ids=lambda cls: cls.__name__)
def test_every_hook_a_shipped_observer_defines_is_guarded(observer: type) -> None:
    """A forgotten ``_safely`` fails the run the day that hook raises (ADR-0026)."""
    hooks = {name: fn for name, fn in vars(observer).items() if name.startswith("on_")}
    assert hooks, f"{observer.__name__} defines no hooks"
    unguarded = [name for name, fn in hooks.items() if not hasattr(fn, "__wrapped__")]
    assert unguarded == []


@pytest.mark.git
async def test_a_run_file_does_not_turn_the_console_up(
    host_repo: Path, tmp_path: Path, clean_logging: None
) -> None:
    """The file wants DEBUG; the console keeps the level the script asked for."""
    console = io.StringIO()
    configure_logging("INFO", console=Console(file=console, width=200))

    result = await a_run(host_repo).hooks(RunLogFiles(tmp_path / "logs"))

    assert isinstance(result, RunSucceeded)
    printed = console.getvalue()
    assert "DEBUG" not in printed, "attaching a run file must not reconfigure stderr"
    assert "workspace ready" in printed, "INFO still reaches the console"
    in_file = (tmp_path / "logs" / f"{result.run_id}.log").read_text(encoding="utf-8")
    assert "DEBUG" in in_file, "the file still gets everything"


@pytest.mark.git
async def test_a_logger_the_script_tuned_still_reaches_the_console(
    host_repo: Path, clean_logging: None
) -> None:
    """Per-logger tuning is plain stdlib and the console filter must respect it."""
    console = io.StringIO()
    configure_logging("INFO", console=Console(file=console, width=200))
    logging.getLogger("waystation.agent.output").setLevel(logging.DEBUG)

    result = await a_run(host_repo)

    assert isinstance(result, RunSucceeded)
    assert "working" in console.getvalue()


@pytest.mark.git
async def test_a_run_that_cannot_read_its_prompt_still_starts(
    host_repo: Path, tmp_path: Path
) -> None:
    """A run never ends without starting: every event log has a first line."""
    # There, so preflight passes it (#30), but not UTF-8, so it can't be read.
    unreadable = tmp_path / "latin1.md"
    unreadable.write_bytes("café".encode("latin-1"))
    events = tmp_path / "events.jsonl"

    result = await a_run(host_repo, unreadable).hooks(EventLog(events))

    assert isinstance(result, RunFailed)
    assert result.stage == "agent", "reading the prompt is still the agent's business"
    assert [event["event"] for event in events_in(events)] == ["run_start", "run_end"]


class _HostHandler(logging.Handler):
    """A host application's own handler, as ``basicConfig`` installs one.

    Its level is NOTSET, so what a host sees is decided by its root logger's
    level — which ``callHandlers`` never rechecks on a propagated record.
    That is why holding the ``waystation`` logger at DEBUG used to put agent
    output into a service's logs.
    """

    def __init__(self) -> None:
        super().__init__(logging.NOTSET)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@contextlib.contextmanager
def _host_logging_at(level: int) -> Iterator[_HostHandler]:
    """What ``logging.basicConfig(level=...)`` leaves behind, put back after."""
    collected = _HostHandler()
    root = logging.getLogger()
    before = root.level
    root.addHandler(collected)
    root.setLevel(level)
    try:
        yield collected
    finally:
        root.removeHandler(collected)
        root.setLevel(before)


@pytest.mark.git
async def test_a_run_file_leaves_a_hosts_own_handlers_at_their_level(
    host_repo: Path, tmp_path: Path, clean_logging: None
) -> None:
    """An open run file must not push agent output into a service's logs.

    The file needs the records to exist, so the ``waystation`` logger goes to
    DEBUG while one is open. Every handler above used to see them too
    (ADR-0026).
    """
    logs = tmp_path / "logs"

    with _host_logging_at(logging.INFO) as collected, RunLogFiles(logs) as files:
        result = await a_run(host_repo).hooks(files)

    assert isinstance(result, RunSucceeded)
    ours = [r for r in collected.records if r.name.startswith("waystation")]
    leaked = [f"{r.name}: {r.getMessage()}" for r in ours if r.levelno < logging.INFO]
    assert leaked == [], "the host was collecting records it never asked for"
    assert ours, "the host still collects what it did ask for"

    # And the file is no poorer for it: the git argv that explains a
    # failure is a DEBUG record, and it is what the file exists to hold.
    text = (logs / f"{result.run_id}.log").read_text(encoding="utf-8")
    assert "DEBUG" in text, "the run file lost the records it is for"
    assert "git " in text, "the git argv at DEBUG belongs in the run file"


@pytest.mark.git
async def test_a_logger_the_script_turned_up_still_reaches_the_host(
    host_repo: Path, tmp_path: Path, clean_logging: None
) -> None:
    """Per-logger tuning is how one stream is turned up; a run file must not
    quietly take it away again.

    ``configure_logging("INFO")`` plus
    ``getLogger("waystation.agent.output").setLevel(DEBUG)`` is the recipe
    ``observability``'s own docstring gives. Those records reach a host
    before a run file opens, so they have to keep reaching it while one is.
    """
    configure_logging("INFO", console=Console(file=io.StringIO()))
    logging.getLogger("waystation.agent.output").setLevel(logging.DEBUG)

    with (
        _host_logging_at(logging.INFO) as collected,
        RunLogFiles(tmp_path / "logs") as files,
    ):
        result = await a_run(host_repo).hooks(files)

    assert isinstance(result, RunSucceeded)
    tuned = [r for r in collected.records if r.name == "waystation.agent.output"]
    assert tuned, "the stream the script turned up stopped reaching the host"
    # And nothing else below INFO came with it.
    others = [
        f"{r.name}: {r.getMessage()}"
        for r in collected.records
        if r.levelno < logging.INFO and r.name != "waystation.agent.output"
    ]
    assert others == []
