"""Flow authoring and the run orchestrator."""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import shutil
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

from pydantic import TypeAdapter
from pydantic.json_schema import GenerateJsonSchema

from waystation.agents.protocol import AgentLine, AgentProvider
from waystation.agents.run_agent import run_agent
from waystation.clock import get_clock, race_timeout
from waystation.collect import CollectResult, collect, preserve_series
from waystation.errors import PreflightError, StageError
from waystation.hooks import (
    HookEntry,
    HookName,
    HookRegistry,
    RunContext,
    RunState,
)
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
    hooks: Sequence[object] = field(default=(), kw_only=True)
    _hook_entries: list[HookEntry] = field(
        init=False, default_factory=list, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        self._hook_entries.extend(HookRegistry().with_bundles(*self.hooks).entries)

    # Decorators: each registers at the flow level and returns ``fn`` unchanged.
    # A run spec snapshots the flow's hooks at ``flow.run()``, so a hook added
    # afterwards never reaches a spec that already exists (ADR-0008).

    def on_run_start[F: Callable[[RunContext], object]](self, fn: F) -> F:
        """Fire ``fn(ctx)`` as a run starts.

        Returns ``fn``; runs built afterwards get it.
        """
        return self._register("run_start", fn)

    def on_workspace_ready[F: Callable[[RunContext], object]](self, fn: F) -> F:
        """Fire ``fn(ctx)`` once the workspace is prepared.

        Returns ``fn``; runs built afterwards get it.
        """
        return self._register("workspace_ready", fn)

    def on_sandbox_ready[F: Callable[[RunContext], object]](self, fn: F) -> F:
        """Fire ``fn(ctx)`` once the sandbox is up, before the agent.

        Returns ``fn``; runs built afterwards get it.
        """
        return self._register("sandbox_ready", fn)

    def on_agent_output[F: Callable[[RunContext, AgentLine], object]](self, fn: F) -> F:
        """Fire ``fn(ctx, line)`` for each line the agent emits.

        Returns ``fn``; runs built afterwards get it.
        """
        return self._register("agent_output", fn)

    def on_agent_end[F: Callable[[RunContext, AgentExit], object]](self, fn: F) -> F:
        """Fire ``fn(ctx, exit)`` when the agent exec ends.

        Returns ``fn``; runs built afterwards get it.
        """
        return self._register("agent_end", fn)

    def on_integrated[F: Callable[[RunContext, IntegrationReport], object]](
        self, fn: F
    ) -> F:
        """Fire ``fn(ctx, report)`` when integration lands.

        Returns ``fn``; runs built afterwards get it.
        """
        return self._register("integrated", fn)

    def on_run_end[F: Callable[[RunContext, RunSucceeded[Any] | RunFailed], object]](
        self, fn: F
    ) -> F:
        """Fire ``fn(ctx, result)`` for every result a run returns.

        Returns ``fn``; runs built afterwards get it.
        """
        return self._register("run_end", fn)

    def _register[F: Callable[..., object]](self, hook: HookName, fn: F) -> F:
        self._hook_entries.append(HookEntry(hook, fn))
        return fn

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
            hook_registry=HookRegistry(tuple(self._hook_entries)),
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
    hook_registry: HookRegistry = field(default_factory=HookRegistry)

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

    # Per-run hooks: each returns a new RunSpec whose hooks fire after the
    # flow's. Bundles are any objects with a subset of the ``on_<hook>`` methods.

    def hooks(self, *bundles: object) -> RunSpec[OutcomeT]:
        """Add each bundle's ``on_<hook>`` methods; returns a new spec."""
        return replace(self, hook_registry=self.hook_registry.with_bundles(*bundles))

    def on_run_start(self, fn: Callable[[RunContext], object]) -> RunSpec[OutcomeT]:
        """Fire ``fn(ctx)`` as a run starts.

        Returns a new spec.
        """
        return self._with_hook("run_start", fn)

    def on_workspace_ready(
        self, fn: Callable[[RunContext], object]
    ) -> RunSpec[OutcomeT]:
        """Fire ``fn(ctx)`` once the workspace is prepared.

        Returns a new spec.
        """
        return self._with_hook("workspace_ready", fn)

    def on_sandbox_ready(self, fn: Callable[[RunContext], object]) -> RunSpec[OutcomeT]:
        """Fire ``fn(ctx)`` once the sandbox is up, before the agent.

        Returns a new spec.
        """
        return self._with_hook("sandbox_ready", fn)

    def on_agent_output(
        self, fn: Callable[[RunContext, AgentLine], object]
    ) -> RunSpec[OutcomeT]:
        """Fire ``fn(ctx, line)`` for each line the agent emits.

        Returns a new spec.
        """
        return self._with_hook("agent_output", fn)

    def on_agent_end(
        self, fn: Callable[[RunContext, AgentExit], object]
    ) -> RunSpec[OutcomeT]:
        """Fire ``fn(ctx, exit)`` when the agent exec ends.

        Returns a new spec.
        """
        return self._with_hook("agent_end", fn)

    def on_integrated(
        self, fn: Callable[[RunContext, IntegrationReport], object]
    ) -> RunSpec[OutcomeT]:
        """Fire ``fn(ctx, report)`` when integration lands.

        Returns a new spec.
        """
        return self._with_hook("integrated", fn)

    def on_run_end(
        self, fn: Callable[[RunContext, RunSucceeded[OutcomeT] | RunFailed], object]
    ) -> RunSpec[OutcomeT]:
        """Fire ``fn(ctx, result)`` for every result a run returns.

        Returns a new spec.
        """
        return self._with_hook("run_end", fn)

    def _with_hook(
        self, hook: HookName, fn: Callable[..., object]
    ) -> RunSpec[OutcomeT]:
        return replace(self, hook_registry=self.hook_registry.with_function(hook, fn))

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
        state = RunState(run_id=secrets.token_hex(4), name=None, repo=self.repo)
        ctx = RunContext(state)
        result = await self._lifecycle(ctx, state)
        end_stage: Stage
        if isinstance(result, RunFailed):
            end_stage = result.stage
        else:
            end_stage = "integrate" if result.report is not None else "collect"
        try:
            # Every run_end hook sees the result, even after one raises.
            await self.hook_registry.fire(
                "run_end", end_stage, ctx, result, stop_on_raise=False
            )
        except StageError as err:
            return _run_failed(
                run_id=result.run_id,
                base_sha=result.base_sha,
                elapsed=dict(result.elapsed),
                agent=result.agent,
                stage=err.stage,
                failure=err.failure,
                series=result.series,
                preserved=result.preserved,
            )
        return result

    async def _lifecycle(
        self, ctx: RunContext, state: RunState
    ) -> RunSucceeded[OutcomeT] | RunFailed:
        hooks = self.hook_registry
        run_id = state.run_id
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
            await hooks.fire("run_start", "workspace", ctx)
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
                    prepare_workspace, self.repo, base=self.base, run_id=run_id
                )

            workspace: Workspace = await self._bounded(
                "workspace", "workspace", timeouts.workspace, _workspace
            )
            elapsed["workspace"] = time.perf_counter() - t0
            base_sha = workspace.base_sha
            state.base_sha = base_sha
            try:
                await hooks.fire("workspace_ready", "workspace", ctx)
            except BaseException:
                # No sandbox owns the workspace yet, so nothing else removes it.
                shutil.rmtree(workspace.path, ignore_errors=True)
                raise

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
                state.sandbox = sandbox
                await hooks.fire("sandbox_ready", "sandbox", ctx)

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

                async def _on_output(line: AgentLine) -> None:
                    await hooks.fire("agent_output", "agent", ctx, line)

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
                        on_output=_on_output,
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

                if agent_exit is None:
                    # Stopped (a bound fired, or a line raised): no exit code.
                    agent_exit = AgentExit(
                        exit_code=-1, elapsed=elapsed["agent"], hanging=False
                    )
                try:
                    await hooks.fire("agent_end", "agent", ctx, agent_exit)
                except StageError as err:
                    agent_failure = err

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
                # The run is done with its sandbox; hooks lose it here (#28).
                state.sandbox = None

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
                    try:
                        await hooks.fire("integrated", "integrate", ctx, report)
                    except StageError as err:
                        # The series landed; a hook failure must not preserve it.
                        return _run_failed(
                            run_id=run_id,
                            base_sha=base_sha,
                            elapsed=elapsed,
                            agent=agent_exit,
                            stage=err.stage,
                            failure=err.failure,
                            series=series,
                        )
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
                state.sandbox = None

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
