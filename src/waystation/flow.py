"""Flow authoring and the run orchestrator."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

from pydantic import TypeAdapter
from pydantic.json_schema import GenerateJsonSchema

from waystation.agents.protocol import AgentProvider
from waystation.agents.run_agent import run_agent
from waystation.clock import get_clock, race_timeout
from waystation.collect import CollectResult, collect, preserve_series
from waystation.errors import PreflightError, StageError
from waystation.integration import Integration, IntegrationStrategy, integrate
from waystation.results import (
    AgentExit,
    Errored,
    Failure,
    IntegrationReport,
    Refused,
    RunFailed,
    RunSucceeded,
    Series,
    Stage,
    Summary,
    TimedOut,
    Timeouts,
)
from waystation.sandbox.protocol import Sandbox, SandboxBackend
from waystation.workspace import Workspace, prepare_workspace


def _assert_object_outcome(outcome_type: type[Any]) -> None:
    adapter = TypeAdapter(outcome_type)
    schema = adapter.json_schema(schema_generator=GenerateJsonSchema)
    # Unwrap trivial refs if present; reject non-object roots.
    while "$ref" in schema and "$defs" in schema:
        ref = schema["$ref"]
        name = ref.rsplit("/", 1)[-1]
        schema = schema["$defs"][name]
    schema_type = schema.get("type")
    if schema_type != "object" and "properties" not in schema:
        msg = f"outcome type must be an object-shaped type, got {outcome_type!r}"
        raise TypeError(msg)


def _run_failed(
    *,
    run_id: str,
    base_sha: str | None,
    elapsed: dict[Stage, float],
    agent: AgentExit | None,
    stage: Stage,
    failure: Failure,
    series: Series | None = None,
    preserved: str | None = None,
) -> RunFailed:
    return RunFailed(
        run_id=run_id,
        name=None,
        base_sha=base_sha,
        elapsed=elapsed,
        agent=agent,
        series=series,
        preserved=preserved,
        stage=stage,
        failure=failure,
    )


@dataclass(frozen=True, slots=True)
class Flow:
    """Defaults shared by runs against one host repo."""

    repo: Path | str
    agent: AgentProvider = field(kw_only=True)
    sandbox: SandboxBackend = field(kw_only=True)
    base: str = field(default="HEAD", kw_only=True)
    integration: IntegrationStrategy | None = field(default=None, kw_only=True)
    timeouts: Timeouts = field(default_factory=Timeouts, kw_only=True)
    salvage: bool = field(default=True, kw_only=True)
    hooks: Sequence[Any] = field(default=(), kw_only=True)

    def run[OutcomeT](
        self,
        prompt: str | Path,
        *,
        outcome: type[OutcomeT] = Summary,  # type: ignore[assignment]
    ) -> RunSpec[OutcomeT]:
        _assert_object_outcome(outcome)
        return RunSpec(
            repo=Path(self.repo),
            agent=self.agent,
            sandbox=self.sandbox,
            base=self.base,
            prompt=prompt,
            outcome_type=outcome,
            timeouts=self.timeouts,
            salvage=self.salvage,
            integration=self.integration,
        )


@dataclass(frozen=True, slots=True)
class RunSpec[OutcomeT]:
    """Frozen description of a run; awaiting it performs the run."""

    repo: Path
    agent: AgentProvider
    sandbox: SandboxBackend
    base: str
    prompt: str | Path
    outcome_type: type[OutcomeT]
    timeouts: Timeouts
    salvage: bool = True
    integration: IntegrationStrategy | None = None

    def integrate(
        self,
        target: str | IntegrationStrategy | None,
        *,
        mechanism: Literal["apply", "merge"] = "apply",
    ) -> RunSpec[OutcomeT]:
        """Set, replace, or clear this run's integration strategy."""
        if target is None:
            strategy: IntegrationStrategy | None = None
        elif isinstance(target, str):
            strategy = Integration(target, mechanism=mechanism)
        else:
            strategy = target
        return replace(self, integration=strategy)

    def with_timeouts(self, timeouts: Timeouts) -> RunSpec[OutcomeT]:
        """Replace this run's timeouts (does not merge with the Flow default).

        Named ``with_timeouts`` rather than ``timeouts`` so it does not shadow
        the ``timeouts`` field (issue #26's ``.timeouts(...)`` spelling).
        """
        return replace(self, timeouts=timeouts)

    def __await__(self):  # type: ignore[no-untyped-def]
        return self._execute().__await__()

    async def _bounded[T](
        self,
        stage: Stage,
        bound: str,
        seconds: float | None,
        factory: Any,
    ) -> T:
        """Run an awaitable factory under a stage bound; raise StageError on expiry."""
        clock = get_clock()
        t0 = clock.monotonic()
        task: asyncio.Task[T] = asyncio.create_task(factory())
        try:
            return await race_timeout(task, seconds)
        except TimeoutError as exc:
            elapsed = clock.monotonic() - t0
            assert seconds is not None
            raise StageError(
                stage,
                TimedOut(bound=bound, limit=seconds, elapsed=elapsed),
            ) from exc

    async def _execute(self) -> RunSucceeded[OutcomeT] | RunFailed:
        run_id = ""
        base_sha: str | None = None
        elapsed: dict[Stage, float] = {}
        agent_exit: AgentExit | None = None
        series: Series | None = None
        preserved: str | None = None
        report: IntegrationReport | None = None
        patch_series = None
        stage: Stage = "workspace"
        timeouts = self.timeouts

        try:
            try:
                self.agent.preflight()
                await self.sandbox.preflight()
            except PreflightError:
                raise
            except Exception as exc:
                raise PreflightError(str(exc), failure=Errored(exception=exc)) from exc

            stage = "workspace"
            t0 = time.perf_counter()

            async def _workspace() -> Workspace:
                return await asyncio.to_thread(
                    prepare_workspace, self.repo, base=self.base
                )

            workspace: Workspace = await self._bounded(
                "workspace", "workspace", timeouts.workspace, _workspace
            )
            elapsed["workspace"] = time.perf_counter() - t0
            run_id = workspace.run_id
            base_sha = workspace.base_sha

            stage = "sandbox"
            t1 = time.perf_counter()

            # start() is an async context manager — bound covers enter only.
            cm = self.sandbox.start(workspace, env={}, pass_env=())

            async def _enter_sandbox() -> Sandbox:
                return await cm.__aenter__()

            try:
                sandbox: Sandbox = await self._bounded(
                    "sandbox", "sandbox", timeouts.sandbox, _enter_sandbox
                )
            except BaseException as exc:
                await cm.__aexit__(type(exc), exc, exc.__traceback__)
                raise
            elapsed["sandbox"] = time.perf_counter() - t1

            try:
                stage = "agent"
                if isinstance(self.prompt, Path):
                    prompt_text = self.prompt.read_text(encoding="utf-8")
                else:
                    prompt_text = self.prompt

                schema = TypeAdapter(self.outcome_type).json_schema()
                try:
                    command = self.agent.command(prompt_text, schema)
                except Exception as exc:
                    raise StageError("agent", Errored(exception=exc)) from exc

                agent_failure: StageError | None = None
                outcome: OutcomeT | None = None
                t2 = time.perf_counter()
                try:
                    agent_exit, outcome = await run_agent(
                        sandbox,
                        self.agent,
                        command,
                        self.outcome_type,
                        timeouts=timeouts,
                    )
                except StageError as err:
                    elapsed["agent"] = time.perf_counter() - t2
                    agent_failure = err
                    if err.agent is not None:
                        agent_exit = err.agent
                except Exception as exc:
                    elapsed["agent"] = time.perf_counter() - t2
                    agent_failure = StageError("agent", Errored(exception=exc))
                else:
                    elapsed["agent"] = time.perf_counter() - t2

                stage = "collect"
                t3 = time.perf_counter()

                async def _collect() -> CollectResult:
                    return await collect(sandbox, workspace, salvage=self.salvage)

                collected: CollectResult = await self._bounded(
                    "collect", "collect", timeouts.collect, _collect
                )
                elapsed["collect"] = time.perf_counter() - t3
                series = collected.series_meta
                patch_series = collected.patch_series

                if collected.squashed:
                    if patch_series.commits > 0:
                        preserved = preserve_series(
                            self.repo,
                            branch=f"waystation/{run_id}",
                            series=patch_series,
                        )
                    return _run_failed(
                        run_id=run_id,
                        base_sha=base_sha,
                        elapsed=elapsed,
                        agent=agent_exit,
                        stage="collect",
                        failure=Refused(
                            reason="nonlinear_series",
                            detail=(
                                "series contains merge commits or "
                                "HEAD does not descend from base"
                            ),
                        ),
                        series=series,
                        preserved=preserved,
                    )

                if agent_failure is not None:
                    if patch_series.commits > 0:
                        preserved = preserve_series(
                            self.repo,
                            branch=f"waystation/{run_id}",
                            series=patch_series,
                        )
                    return _run_failed(
                        run_id=run_id,
                        base_sha=base_sha,
                        elapsed=elapsed,
                        agent=(
                            agent_failure.agent
                            if agent_failure.agent is not None
                            else agent_exit
                        ),
                        stage=agent_failure.stage,
                        failure=agent_failure.failure,
                        series=series,
                        preserved=preserved,
                    )

                assert outcome is not None

                if self.integration is not None:
                    stage = "integrate"
                    t4 = time.perf_counter()
                    strategy = self.integration

                    async def _integrate() -> IntegrationReport:
                        return await integrate(self.repo, patch_series, strategy)

                    report = await self._bounded(
                        "integrate", "integrate", timeouts.integrate, _integrate
                    )
                    elapsed["integrate"] = time.perf_counter() - t4
                    preserved = None
                elif patch_series.commits > 0:
                    preserved = preserve_series(
                        self.repo,
                        branch=f"waystation/{run_id}",
                        series=patch_series,
                    )

                return RunSucceeded(
                    run_id=run_id,
                    name=None,
                    base_sha=base_sha,
                    elapsed=elapsed,
                    agent=agent_exit,
                    series=series,
                    preserved=preserved,
                    outcome=outcome,
                    report=report,
                )
            finally:
                # Teardown bound (optional); hooks are never bounded here.
                async def _leave() -> None:
                    await cm.__aexit__(None, None, None)

                t_td = time.perf_counter()
                try:
                    await self._bounded(
                        "sandbox", "teardown", timeouts.teardown, _leave
                    )
                except StageError:
                    # Teardown timeout still surfaces as RunFailed if nothing else.
                    raise
                finally:
                    if timeouts.teardown is not None:
                        elapsed.setdefault("sandbox", time.perf_counter() - t_td)
        except StageError as err:
            if (
                preserved is None
                and patch_series is not None
                and patch_series.commits > 0
                and err.stage == "integrate"
            ):
                with contextlib.suppress(Exception):
                    preserved = preserve_series(
                        self.repo,
                        branch=f"waystation/{run_id}",
                        series=patch_series,
                    )
            return _run_failed(
                run_id=run_id,
                base_sha=base_sha,
                elapsed=elapsed,
                agent=err.agent if err.agent is not None else agent_exit,
                stage=err.stage,
                failure=err.failure,
                series=series,
                preserved=preserved,
            )
        except PreflightError as err:
            failure: Failure = (
                err.failure if err.failure is not None else Errored(exception=err)
            )
            return _run_failed(
                run_id=run_id,
                base_sha=base_sha,
                elapsed=elapsed,
                agent=agent_exit,
                stage=stage,
                failure=failure,
                series=series,
                preserved=preserved,
            )
        except Exception as exc:
            return _run_failed(
                run_id=run_id,
                base_sha=base_sha,
                elapsed=elapsed,
                agent=agent_exit,
                stage=stage,
                failure=Errored(exception=exc),
                series=series,
                preserved=preserved,
            )
