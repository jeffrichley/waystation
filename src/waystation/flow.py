"""Flow authoring and the run orchestrator."""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import time
from collections.abc import Awaitable, Callable, Iterator, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

from pydantic import TypeAdapter
from pydantic.json_schema import GenerateJsonSchema

from waystation._cancellation import run_to_end, start_bounded
from waystation._preflight import preflight
from waystation.agents.protocol import AgentLine, AgentProvider
from waystation.agents.run_agent import run_agent
from waystation.clock import get_clock, race_timeout
from waystation.collect import PatchSeries, collect
from waystation.errors import StageError
from waystation.hooks import (
    HookEntry,
    HookName,
    HookRegistry,
    RunContext,
    RunState,
)
from waystation.integration import (
    Integration,
    IntegrationStrategy,
    integrate,
    preserve_series,
)
from waystation.observability import bind_run, tagged_logger
from waystation.observers import RunLog
from waystation.results import (
    AgentExit,
    Errored,
    IntegrationReport,
    RunConflicted,
    RunFailed,
    RunResult,
    RunSucceeded,
    Series,
    Stage,
    Summary,
    TimedOut,
    Timeouts,
)
from waystation.sandbox.protocol import Sandbox, SandboxBackend
from waystation.workspace import Workspace, prepare_workspace, remove_workspace

__all__ = ["Flow", "RunSpec"]

logger = tagged_logger("waystation")


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


def _log_unreported(run_id: str, err: StageError, why: str) -> None:
    """Log a failure no result will carry, saying ``why`` it goes unreported."""
    exception = getattr(err.failure, "exception", None)
    logger.error(
        "run %s: %s failed %s: %r",
        run_id,
        err.stage,
        why,
        err.failure,
        exc_info=exception,
    )


def _log_later_failure(run_id: str, err: StageError) -> None:
    """Log a failure met after the run already failed; never report it (ADR-0024)."""
    _log_unreported(run_id, err, "after the run had already failed")


def _as_stage_error(stage: Stage, exc: Exception) -> StageError:
    return exc if isinstance(exc, StageError) else StageError(stage, Errored(exc))


@dataclass(slots=True)
class _RunRecord:
    """What a run has gathered so far, and the one failure it will report."""

    run_id: str
    log: RunLog
    stage: Stage = "workspace"
    base_sha: str | None = None
    elapsed: dict[Stage, float] = field(default_factory=dict)
    agent: AgentExit | None = None
    series: Series | None = None
    patches: PatchSeries | None = None
    preserved: str | None = None
    landed_on: str | None = None
    failure: StageError | None = None
    # A cancellation held until the stage it arrived during has done its work.
    held: tuple[Stage, asyncio.CancelledError] | None = None

    @contextlib.contextmanager
    def entering(self, stage: Stage) -> Iterator[None]:
        """Enter ``stage``; its elapsed time is recorded however it ends."""
        self.stage = stage
        started = time.perf_counter()
        try:
            yield
        finally:
            self.elapsed[stage] = time.perf_counter() - started

    def collected(self, series: PatchSeries) -> None:
        """Keep what collect cut, whether or not it went on to refuse it."""
        self.patches = series
        self.series = Series(commits=series.commits, salvaged=series.salvaged)

    def fail(self, err: StageError) -> None:
        """Keep the first failure; log any later one (ADR-0024)."""
        if self.failure is not None:
            _log_later_failure(self.run_id, err)
            return
        self.failure = err
        if err.agent is not None:
            self.agent = err.agent

    def hold(self, stage: Stage, cancel: asyncio.CancelledError) -> None:
        """Keep the first cancellation until ``surface`` lets it go (ADR-0017)."""
        if self.held is None:
            self.held = (stage, cancel)

    async def uninterrupted[T](self, stage: Stage, work: Awaitable[T]) -> T:
        """Await ``work`` to its end; a cancellation meanwhile is held, not lost."""
        return await run_to_end(work, lambda cancel: self.hold(stage, cancel))

    def surface(self) -> None:
        """Raise the held cancellation, if any: the stage it waited on is done."""
        if self.held is not None:
            raise self.held[1]

    def log_cancelled(self) -> None:
        """Say where a cancelled run's series went: no ``run_end`` will."""
        if self.failure is not None:
            _log_unreported(self.run_id, self.failure, "in a run that was cancelled")
        stage = self.held[0] if self.held is not None else self.stage
        self.log.cancelled(stage, kept_on=self.preserved, landed_on=self.landed_on)

    def failed(self) -> RunFailed:
        assert self.failure is not None
        return RunFailed(
            run_id=self.run_id,
            name=None,
            base_sha=self.base_sha,
            elapsed=dict(self.elapsed),
            agent=self.agent,
            series=self.series,
            preserved=self.preserved,
            stage=self.failure.stage,
            failure=self.failure.failure,
        )

    def succeeded[OutcomeT](
        self, outcome: OutcomeT, report: IntegrationReport | None
    ) -> RunSucceeded[OutcomeT]:
        return RunSucceeded(
            run_id=self.run_id,
            name=None,
            base_sha=self.base_sha,
            elapsed=dict(self.elapsed),
            agent=self.agent,
            series=self.series,
            preserved=self.preserved,
            outcome=outcome,
            report=report,
        )

    def conflicted[OutcomeT](
        self, outcome: OutcomeT, report: IntegrationReport
    ) -> RunConflicted[OutcomeT]:
        return RunConflicted(
            run_id=self.run_id,
            name=None,
            base_sha=self.base_sha,
            elapsed=dict(self.elapsed),
            agent=self.agent,
            series=self.series,
            preserved=self.preserved,
            outcome=outcome,
            report=report,
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

    def on_run_end[F: Callable[[RunContext, RunResult[Any]], object]](self, fn: F) -> F:
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
        self,
        fn: Callable[
            [RunContext, RunResult[OutcomeT]],
            object,
        ],
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
        record: _RunRecord,
        stage: Stage,
        bound: str,
        seconds: float | None,
        factory: Any,
    ) -> T:
        """Run a stage's own work under its bound; raise StageError on expiry.

        Cancelling the run never interrupts this work — a half-cloned
        workspace, half-cut series or half-moved target would outlive it — so
        the cancellation is held on ``record`` until the work ends (ADR-0017).
        """
        clock = get_clock()
        t0 = clock.monotonic()
        task, commits = start_bounded(factory())
        bounded: Awaitable[T] = race_timeout(task, seconds, commits=commits)
        try:
            return await record.uninterrupted(stage, bounded)
        except TimeoutError as exc:
            elapsed = clock.monotonic() - t0
            assert seconds is not None
            raise StageError(
                stage,
                TimedOut(bound=bound, limit=seconds, elapsed=elapsed),
            ) from exc

    async def _execute(self, *, preflighted: bool = False) -> RunResult[OutcomeT]:
        """Perform one run; ``preflighted`` when fan-out checked its batch whole."""
        state = RunState(run_id=secrets.token_hex(4), name=None, repo=self.repo)
        ctx = RunContext(state)
        record = _RunRecord(run_id=state.run_id, log=RunLog(state.run_id, state.name))
        with bind_run(state.run_id, state.name):
            # Bound, so what preflight logs carries the id, but a run that
            # fails it never began: no hook fires and no workspace is made.
            # A lone run is a batch of one, checked the way fan-out checks
            # a batch (#30).
            if not preflighted:
                await preflight((self,))
            record.log.on_run_start(ctx)
            try:
                result = await self._lifecycle(ctx, state, record)
                end_stage = (
                    result.stage if isinstance(result, RunFailed) else record.stage
                )
                try:
                    # Every run_end hook sees the result, even after one raises.
                    await self.hook_registry.fire(
                        "run_end", end_stage, ctx, result, stop_on_raise=False
                    )
                except StageError as err:
                    record.fail(err)
                    result = record.failed()
            except asyncio.CancelledError:
                # Logged, never reported: there is no Cancelled result (ADR-0017).
                record.log_cancelled()
                raise
            record.log.on_run_end(ctx, result)
            return result

    async def _lifecycle(
        self, ctx: RunContext, state: RunState, record: _RunRecord
    ) -> RunResult[OutcomeT]:
        """Run the stages in order; each phase below owns its own stages."""
        try:
            # run_start fires for every run, so a prompt file that cannot be
            # read waits its turn: the run started, and then it failed.
            prompt_error: StageError | None = None
            try:
                state.prompt = self._prompt_text()
            except StageError as err:
                prompt_error = err
            await self.hook_registry.fire("run_start", "workspace", ctx)
            if prompt_error is not None:
                raise prompt_error
            workspace = await self._prepare(ctx, state, record)
            outcome = await self._in_sandbox(ctx, state, record, workspace)
            if record.failure is not None or record.held is not None:
                await self._preserve(record)
                record.surface()  # before integrate starts: it never will
                return record.failed()
            assert outcome is not None
            return await self._land(ctx, record, outcome)
        except Exception as exc:
            record.fail(_as_stage_error(record.stage, exc))
        record.surface()  # a failure never hides a cancellation held meanwhile
        return record.failed()

    def _prompt_text(self) -> str:
        """The prompt as text, whether the flow script gave a string or a path.

        Resolved before ``run_start`` so every hook sees ``ctx.prompt``, but a
        file that cannot be read still fails the agent stage: preparing the
        agent's prompt is the agent's business, whenever it happens.
        """
        if not isinstance(self.prompt, Path):
            return self.prompt
        try:
            return self.prompt.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise StageError("agent", Errored(exception=exc)) from exc

    async def _prepare(
        self, ctx: RunContext, state: RunState, record: _RunRecord
    ) -> Workspace:
        """Workspace stage: a private clone of the base, then ``workspace_ready``."""

        async def _workspace() -> Workspace:
            return await prepare_workspace(
                self.repo, base=self.base, run_id=record.run_id
            )

        with record.entering("workspace"):
            workspace: Workspace = await self._bounded(
                record, "workspace", "workspace", self.timeouts.workspace, _workspace
            )
        record.base_sha = state.base_sha = workspace.base_sha
        try:
            record.surface()  # held while the workspace was cloned
            record.log.on_workspace_ready(ctx)
            await self.hook_registry.fire("workspace_ready", "workspace", ctx)
        except BaseException:
            # No sandbox owns the workspace yet, so nothing else removes it.
            with contextlib.suppress(OSError):
                remove_workspace(workspace.path)
            raise
        return workspace

    async def _in_sandbox(
        self,
        ctx: RunContext,
        state: RunState,
        record: _RunRecord,
        workspace: Workspace,
    ) -> OutcomeT | None:
        """Sandbox, agent and collect stages; the sandbox is gone on return."""
        # start() is an async context manager — the bound covers enter only.
        cm = self.sandbox.start(workspace, env={}, pass_env=())

        async def _enter() -> Sandbox:
            return await cm.__aenter__()

        with record.entering("sandbox"):
            try:
                sandbox: Sandbox = await self._bounded(
                    record, "sandbox", "sandbox", self.timeouts.sandbox, _enter
                )
            except BaseException:
                # A start that failed cleans up after itself: ``async with``
                # never exits a context it did not enter, and exiting a
                # generator that raised fails with a RuntimeError of its own.
                # Only a start its bound cancelled before it ran leaves the
                # workspace behind, so no sandbox ever owned it.
                with contextlib.suppress(OSError):
                    if workspace.path.exists():
                        remove_workspace(workspace.path)
                raise
        try:
            record.surface()  # held while the sandbox started
            state.sandbox = sandbox
            record.log.on_sandbox_ready(ctx)
            await self.hook_registry.fire("sandbox_ready", "sandbox", ctx)
            try:
                outcome = await self._run_agent(ctx, record, sandbox)
            except asyncio.CancelledError as cancel:
                # The exec killed the agent's tree before this surfaced
                # (ADR-0023), so what it left is collected like any stopped
                # agent's; the cancellation waits for that (ADR-0017).
                record.hold("agent", cancel)
                outcome = None
            await self._collect(record, sandbox, workspace)
            return outcome
        finally:
            state.sandbox = None
            await self._teardown(cm, record)

    async def _run_agent(
        self, ctx: RunContext, record: _RunRecord, sandbox: Sandbox
    ) -> OutcomeT | None:
        """Agent stage: exec the provider's command, then ``agent_end``."""
        record.stage = "agent"
        prompt_text = ctx.prompt
        schema = TypeAdapter(self.outcome_type).json_schema()
        try:
            command = self.agent.command(prompt_text, schema)
        except Exception as exc:
            raise StageError("agent", Errored(exception=exc)) from exc

        record.log.agent_start(prompt_text)

        async def _on_output(line: AgentLine) -> None:
            record.log.on_agent_output(ctx, line)
            await self.hook_registry.fire("agent_output", "agent", ctx, line)

        outcome: OutcomeT | None = None
        with record.entering("agent"):
            try:
                record.agent, outcome = await run_agent(
                    sandbox,
                    self.agent,
                    command,
                    self.outcome_type,
                    timeouts=self.timeouts,
                    on_output=_on_output,
                )
            except Exception as exc:
                record.fail(_as_stage_error("agent", exc))
        if record.agent is None:
            # A line raised, so run_agent had no exit to report: no exit code.
            record.agent = AgentExit(
                exit_code=-1, elapsed=record.elapsed["agent"], hanging=False
            )
        record.log.on_agent_end(ctx, record.agent)
        try:
            await self.hook_registry.fire("agent_end", "agent", ctx, record.agent)
        except StageError as err:
            record.fail(err)
        return outcome

    async def _collect(
        self, record: _RunRecord, sandbox: Sandbox, workspace: Workspace
    ) -> None:
        """Collect stage: best-effort once the run has already failed (ADR-0024)."""

        async def _collected() -> PatchSeries:
            return await collect(sandbox, workspace, salvage=self.salvage)

        with record.entering("collect"):
            try:
                record.collected(
                    await self._bounded(
                        record, "collect", "collect", self.timeouts.collect, _collected
                    )
                )
            except Exception as exc:
                err = _as_stage_error("collect", exc)
                # A stage hands over what it made before it failed, and collect
                # cut a series before it refused it: that is what preservation
                # keeps, whether or not this failure is the one reported.
                if err.series is not None:
                    record.collected(err.series)
                record.fail(err)

    async def _land(
        self, ctx: RunContext, record: _RunRecord, outcome: OutcomeT
    ) -> RunResult[OutcomeT]:
        """Integrate stage, or preservation when the run integrates nowhere."""
        patches, strategy = record.patches, self.integration
        assert patches is not None
        if strategy is None:
            await self._preserve(record)
            record.surface()  # held while the series was kept
            return (
                record.failed() if record.failure else record.succeeded(outcome, None)
            )

        async def _integrated() -> IntegrationReport:
            return await integrate(self.repo, patches, strategy)

        with record.entering("integrate"):
            try:
                report: IntegrationReport = await self._bounded(
                    record,
                    "integrate",
                    "integrate",
                    self.timeouts.integrate,
                    _integrated,
                )
            except Exception as exc:
                record.fail(_as_stage_error("integrate", exc))
        if record.failure is not None or report.conflict is not None:
            # Nothing landed, so the series is kept like any other that
            # reached no target (ADR-0005).
            await self._preserve(record)
        else:
            record.landed_on = report.target
        # The stage was atomic: a cancellation held meanwhile surfaces only
        # now, with the target moved or left alone, never half-moved.
        record.surface()
        if record.failure is not None:
            return record.failed()
        if report.conflict is not None:
            # No ``integrated`` hook: the conflict rides ``run_end`` (ADR-0015).
            return record.conflicted(outcome, report)
        record.log.on_integrated(ctx, report)
        try:
            await self.hook_registry.fire("integrated", "integrate", ctx, report)
        except StageError as err:
            # The series landed; a hook failure must not preserve it.
            record.fail(err)
            return record.failed()
        return record.succeeded(outcome, report)

    async def _preserve(self, record: _RunRecord) -> None:
        """Keep a series that reached no target on ``waystation/<run-id>``.

        Unbounded and never interrupted: preservation is how a cancelled or
        failed run loses nothing (ADR-0016, ADR-0017).
        """
        if record.patches is None or record.patches.commits == 0:
            return
        branch = f"waystation/{record.run_id}"
        try:
            record.preserved = await record.uninterrupted(
                "integrate",
                preserve_series(self.repo, branch=branch, series=record.patches),
            )
        except Exception as exc:
            record.fail(_as_stage_error("integrate", exc))

    async def _teardown(
        self, cm: AbstractAsyncContextManager[Sandbox], record: _RunRecord
    ) -> None:
        """Leave the sandbox; a failure is logged, never reported (ADR-0016)."""

        async def _leave() -> None:
            await cm.__aexit__(None, None, None)

        try:
            await self._bounded(
                record, "sandbox", "teardown", self.timeouts.teardown, _leave
            )
        except Exception as exc:
            _log_later_failure(record.run_id, _as_stage_error("sandbox", exc))
