"""NoSandbox takes what is OS-specific about processes as an injected strategy."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from helpers import OUTCOME, ShellAgent
from waystation import (
    Flow,
    NoSandbox,
    RunContext,
    RunFailed,
    RunSucceeded,
    ScriptedAgent,
)
from waystation.agents import (
    AgentLine,
)
from waystation.sandbox import (
    HostRunner,
    ProcessStrategy,
    ProcessTree,
    host_processes,
    host_shell,
)


class Answer(BaseModel):
    summary: str


class _RecordedTree:
    def __init__(self, inner: ProcessTree, events: list[str]) -> None:
        self.inner = inner
        self.events = events

    def kill(self) -> None:
        self.events.append("kill")
        self.inner.kill()

    def release(self) -> None:
        self.events.append("release")
        self.inner.release()


class RecordingProcesses:
    """Wraps the host's strategy, recording calls and widening the env allowlist."""

    def __init__(self, base_env_keys: Sequence[str] | None = None) -> None:
        self.inner = host_processes()
        self.events: list[str] = []
        self._base_env_keys = base_env_keys

    def base_env_keys(self) -> Sequence[str]:
        if self._base_env_keys is None:
            return self.inner.base_env_keys()
        return self._base_env_keys

    def spawn_options(self) -> dict[str, Any]:
        return self.inner.spawn_options()

    def adopt(self, process: asyncio.subprocess.Process) -> ProcessTree:
        self.events.append("adopt")
        return _RecordedTree(self.inner.adopt(process), self.events)


@pytest.mark.git
@pytest.mark.asyncio
async def test_injected_strategy_kills_a_stopped_agent_and_releases_every_exec(
    host_repo: Path,
) -> None:
    processes = RecordingProcesses()
    flow = Flow(
        host_repo,
        agent=ScriptedAgent(lines=["first"], linger=True),
        sandbox=NoSandbox(processes=processes),
    )

    def stop(ctx: RunContext, line: AgentLine) -> None:
        raise RuntimeError("stop the agent")

    result = await flow.run("stop", outcome=Answer).on_agent_output(stop)

    assert isinstance(result, RunFailed)
    assert processes.events.count("kill") == 1
    assert processes.events.count("release") == processes.events.count("adopt")
    last_release = len(processes.events) - 1 - processes.events[::-1].index("release")
    assert processes.events.index("kill") < last_release


@pytest.mark.git
@pytest.mark.asyncio
async def test_the_strategy_decides_which_host_env_a_process_starts_with(
    host_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WAYSTATION_PROBE", "leaked")
    widened = RecordingProcesses(
        (*host_processes().base_env_keys(), "WAYSTATION_PROBE")
    )
    agent = ShellAgent(
        "; ".join(
            [
                'echo "probe=${WAYSTATION_PROBE:-none}"',
                f"echo '{OUTCOME}" + '{"summary": "ok"}' + "'",
            ]
        )
    )

    seen: list[str] = []
    for sandbox in (NoSandbox(), NoSandbox(processes=widened)):
        result = await (
            Flow(host_repo, agent=agent, sandbox=sandbox)
            .run("probe", outcome=Answer)
            .on_agent_output(lambda ctx, line: seen.append(line.raw))
        )
        assert isinstance(result, RunSucceeded)

    assert "probe=none" in seen
    assert "probe=leaked" in seen


class _SlowToAdopt:
    """The real strategy, but slow to take charge — which is what load does.

    Taking charge happens after ``create_subprocess_exec`` returns, and that
    was measured at 150 ms to 3 s on a loaded host. This makes that window a
    number instead of a race, so the test below means the same thing on an
    idle laptop as on a four-core runner (#105).
    """

    def __init__(self, inner: ProcessStrategy, window: float) -> None:
        self.inner = inner
        self.window = window

    def base_env_keys(self) -> Sequence[str]:
        return self.inner.base_env_keys()

    def spawn_options(self) -> dict[str, Any]:
        return self.inner.spawn_options()

    def adopt(self, process: asyncio.subprocess.Process) -> ProcessTree:
        time.sleep(self.window)  # blocking, as a loaded event loop is
        return self.inner.adopt(process)


@pytest.mark.git
async def test_cancelling_an_exec_kills_what_its_agent_started_too(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """#105: a descendant that outlives the kill parks the cancellation for ever.

    The agent backgrounds a long sleep, which inherits its stdout, then works.
    A descendant spawned before the exec is in its job is outside it, survives
    its kill, and goes on holding that pipe — and ``Process.wait()`` on Windows
    returns once every pipe has closed rather than once the process has exited
    (cpython gh-119710, on 3.12 through 3.14). So the cancellation never
    finished, and everything waiting on the run waited with it.

    Two claims, and the second is the one that bites: the cancellation ends,
    *and* it ends because the tree died, not because the runner gave up on it
    after the grace. Strategy-level, because that is where the fix is: the
    child is created suspended, so during the window below it cannot fork.
    """
    runner = HostRunner(processes=_SlowToAdopt(host_processes(), window=0.5))
    ready = asyncio.Event()

    def watch(line: str) -> None:
        if line.strip() == "started":
            ready.set()

    script = "sleep 120 & echo started; while true; do sleep 0.05; done"
    exec_task = asyncio.create_task(
        runner.run([*host_shell(), script], capture=False, on_stdout=watch)
    )
    with caplog.at_level(logging.ERROR, logger="waystation"):
        await asyncio.wait_for(ready.wait(), timeout=60)
        exec_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            # Unbounded before the fix. The bound is the assertion.
            await asyncio.wait_for(asyncio.shield(exec_task), timeout=60)
    runner.release()

    abandoned = [r for r in caplog.records if "outlived its kill" in r.getMessage()]
    assert abandoned == [], (
        "the tree died, rather than the runner giving up on it after the grace"
    )
