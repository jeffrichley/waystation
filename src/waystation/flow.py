"""Flow authoring and the run orchestrator."""

from __future__ import annotations

import asyncio
import contextlib
import os
import secrets
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

from pydantic import TypeAdapter
from pydantic.json_schema import GenerateJsonSchema

from waystation.agents.protocol import AgentLine, AgentProvider
from waystation.agents.run_agent import run_agent
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
from waystation.preflight import preflight
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
    Timeouts,
)
from waystation.sandbox.protocol import Sandbox, SandboxBackend
from waystation.stages import StageRunner, stages
from waystation.workspace import Workspace, prepare_workspace, remove_workspace

__all__ = ["Flow", "RunSpec"]

_logger = tagged_logger("waystation")


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
    _logger.error(
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
    """``exc`` as a failure of ``stage``, leaving a stage it already names alone.

    It claims the stage here rather than leaving it to ``fail``, because the
    teardown path logs its failure without ever reaching a record.
    """
    if isinstance(exc, StageError):
        return exc.at(stage)
    return StageError(stage, Errored(exc))


@dataclass(slots=True)
class _RunRecord:
    """What a run has gathered so far, and the one failure it will report.

    The bound each stage ran under, the cancellation held until that stage's
    work ended and the time it took are not here: they are the stage
    runner's, and this run is its first user, not a privileged path
    (ADR-0032). ``elapsed`` is the runner's own live view.
    """

    run_id: str
    log: RunLog
    # Where the orchestrator is *between* stages — reading the prompt, firing
    # a hook — which is work the runner never sees, so it cannot name it. Each
    # phase below sets it as it begins; inside a stage the runner attributes.
    stage: Stage = "workspace"
    base_sha: str | None = None
    elapsed: Mapping[Stage, float] = field(default_factory=dict)
    agent: AgentExit | None = None
    series: Series | None = None
    patches: PatchSeries | None = None
    preserved: str | None = None
    landed_on: str | None = None
    failure: StageError | None = None
    # The stage a cancellation arrived during, once the runner has held one.
    cancelled_during: Stage | None = None

    def collected(self, series: PatchSeries) -> None:
        """Keep what collect cut, whether or not it went on to refuse it."""
        self.patches = series
        self.series = Series(commits=series.commits, salvaged=series.salvaged)

    def fail(self, err: StageError) -> None:
        """Keep the first failure; log any later one (ADR-0024)."""
        # The net that lets ``failed()`` assert a stage: a failure raised
        # between stages, by a hook say, is claimed by the stage under way.
        err.at(self.stage)
        if self.failure is not None:
            _log_later_failure(self.run_id, err)
            return
        self.failure = err
        if err.agent is not None:
            self.agent = err.agent

    def log_cancelled(self) -> None:
        """Say where a cancelled run's series went: no ``run_end`` will."""
        if self.failure is not None:
            _log_unreported(self.run_id, self.failure, "in a run that was cancelled")
        stage = self.cancelled_during or self.stage
        self.log.cancelled(stage, kept_on=self.preserved, landed_on=self.landed_on)

    def failed(self) -> RunFailed:
        assert self.failure is not None
        assert self.failure.stage is not None  # fail() attributes every failure
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
            provider=self.agent,
            backend=self.sandbox,
            base_ref=self.base,
            prompt=prompt,
            outcome_type=outcome,
            bounds=self.timeouts,
            salvaging=self.salvage,
            integration=self.integration,
            hook_registry=HookRegistry(tuple(self._hook_entries)),
        )


@dataclass(frozen=True, slots=True)
class RunSpec[OutcomeT]:
    """Frozen description of a run; awaiting it performs the run.

    Build one with ``flow.run(prompt)``, never by calling this: a spec that
    grows a field later is then not a breaking change (ADR-0031). Chain the
    builders to describe the run — each returns a *new* spec, so the one you
    started from is unchanged (ADR-0022)::

        spec = flow.run("fix the flake").agent(ClaudeCode()).base("main")

    The builders own the bare names, and the values they set are stored under
    the words ``CONTEXT.md`` uses for them. Every attribute below is public
    and read-only — a scheduler may group a batch by ``spec.backend``, and
    nothing may write one:

    Attributes:
        repo: The host repo this run targets.
        provider: The agent provider that will run — ``.agent()`` sets it.
        backend: The sandbox backend it runs in — ``.sandbox()`` sets it.
        base_ref: The ref the workspace starts from — ``.base()`` sets it.
        prompt: The instructions handed to the agent, or a path to them.
        outcome_type: The object-shaped type the agent reports back.
        bounds: The per-stage ``Timeouts`` — ``.timeouts()`` sets them.
        salvaging: Whether work left uncommitted is salvaged —
            ``.salvage()`` sets it.
        integration: Where the series lands, or ``None`` to land nowhere —
            ``.integrate()`` sets it.
        hook_registry: The hooks that fire — ``.hooks()`` and ``.on_<hook>()``
            append to it.
    """

    repo: Path
    provider: AgentProvider
    backend: SandboxBackend
    base_ref: str
    prompt: str | Path
    outcome_type: type[OutcomeT]
    bounds: Timeouts
    salvaging: bool = True
    integration: IntegrationStrategy | None = None
    hook_registry: HookRegistry = field(default_factory=HookRegistry)

    # Builders. Each replaces the value it names and returns a new spec, the
    # way ``dataclasses.replace`` does; the hook builders below append instead
    # (ADR-0022, ADR-0031).

    def agent(self, provider: AgentProvider) -> RunSpec[OutcomeT]:
        """Replace the agent provider this run executes.

        Args:
            provider: The provider to run instead of the flow's.

        Returns:
            A new spec; this one is unchanged.
        """
        return replace(self, provider=provider)

    def sandbox(self, backend: SandboxBackend) -> RunSpec[OutcomeT]:
        """Replace the sandbox backend this run executes in.

        Args:
            backend: The backend to run in instead of the flow's.

        Returns:
            A new spec; this one is unchanged.
        """
        return replace(self, backend=backend)

    def base(self, ref: str) -> RunSpec[OutcomeT]:
        """Replace the ref this run's workspace starts from.

        Args:
            ref: The base ref, resolved to a sha when the run starts, so a
                batch queued behind a slot picks up where the repo is then.

        Returns:
            A new spec; this one is unchanged.
        """
        return replace(self, base_ref=ref)

    def salvage(self, salvaging: bool = True) -> RunSpec[OutcomeT]:
        """Say whether work the agent left uncommitted is salvaged.

        Args:
            salvaging: ``True`` to commit what the agent left as a final
                salvage commit on the series; ``False`` to leave it behind.

        Returns:
            A new spec; this one is unchanged.
        """
        return replace(self, salvaging=salvaging)

    def integrate(
        self,
        target: str | IntegrationStrategy | None,
        *,
        mechanism: Literal["apply", "merge"] = "apply",
    ) -> RunSpec[OutcomeT]:
        """Set, replace, or clear this run's integration strategy.

        Args:
            target: A branch name, any ``IntegrationStrategy``, or ``None``
                to land nowhere and keep the series instead.
            mechanism: ``"apply"`` replays the series commit by commit;
                ``"merge"`` lands it in one step. Ignored unless ``target``
                is a branch name.

        Returns:
            A new spec; this one is unchanged.
        """
        if target is None:
            strategy: IntegrationStrategy | None = None
        elif isinstance(target, str):
            strategy = Integration(target, mechanism=mechanism)
        else:
            strategy = target
        return replace(self, integration=strategy)

    def timeouts(self, bounds: Timeouts) -> RunSpec[OutcomeT]:
        """Replace this run's per-stage bounds.

        The whole value is replaced, not merged with the flow's default: a
        ``Timeouts`` says what every stage's bound is, so merging would leave
        a bound set somewhere the caller cannot see.

        Args:
            bounds: The bounds to run under. Every field defaults to
                unbounded (ADR-0017).

        Returns:
            A new spec; this one is unchanged.
        """
        return replace(self, bounds=bounds)

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
        return self.perform().__await__()

    async def perform(self, *, preflighted: bool = False) -> RunResult[OutcomeT]:
        """Perform one run, with a new run id; what ``await spec`` does.

        Call it yourself only to say ``preflighted=True``. A scheduler of your
        own — a priority queue, a retry pool, a ``TaskGroup`` — checks its
        batch once with ``preflight(specs)`` and then performs each spec
        already checked, instead of re-checking docker, image and host git per
        run. That is what fan-out does, through this same call (ADR-0032).

        Args:
            preflighted: The batch this spec belongs to has already passed
                ``preflight``, so this run skips it. Passing it for a spec
                nothing checked skips the check entirely.

        Returns:
            ``RunSucceeded``, ``RunConflicted`` or ``RunFailed``: a failure is
            a value here, never a raise (ADR-0016).

        Raises:
            PreflightError: When the check this run does for itself fails; the
                run never began.
            asyncio.CancelledError: When the run was cancelled. Nothing
                reports a cancelled run, but what its agent left is still
                collected and kept first (ADR-0017).
        """
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
                result = await self._staged(ctx, state, record)
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

    async def _staged(
        self, ctx: RunContext, state: RunState, record: _RunRecord
    ) -> RunResult[OutcomeT]:
        """Run the lifecycle through one stage runner, which holds the guarantees.

        Every bound, every held cancellation and every elapsed time below
        comes from ``run``; what is left here is this orchestrator's policy
        (ADR-0032).
        """
        async with stages(self.bounds) as run:
            record.elapsed = run.elapsed
            try:
                return await self._lifecycle(run, ctx, state, record)
            finally:
                record.cancelled_during = run.cancelled_during

    async def _lifecycle(
        self,
        run: StageRunner,
        ctx: RunContext,
        state: RunState,
        record: _RunRecord,
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
            workspace = await self._prepare(run, ctx, state, record)
            outcome = await self._in_sandbox(run, ctx, state, record, workspace)
            if record.failure is not None or run.cancelled_during is not None:
                await self._preserve(run, record)
                run.surface()  # before integrate starts: it never will
                return record.failed()
            assert outcome is not None
            return await self._land(run, ctx, record, outcome)
        except Exception as exc:
            record.fail(_as_stage_error(record.stage, exc))
        run.surface()  # a failure never hides a cancellation held meanwhile
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
        self, run: StageRunner, ctx: RunContext, state: RunState, record: _RunRecord
    ) -> Workspace:
        """Workspace stage: a private clone of the base, then ``workspace_ready``."""
        record.stage = "workspace"
        workspace = await run.stage(
            "workspace",
            prepare_workspace(self.repo, base=self.base_ref, run_id=record.run_id),
        )
        record.base_sha = state.base_sha = workspace.base_sha
        try:
            run.surface()  # held while the workspace was cloned
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
        run: StageRunner,
        ctx: RunContext,
        state: RunState,
        record: _RunRecord,
        workspace: Workspace,
    ) -> OutcomeT | None:
        """Sandbox, agent and collect stages; the sandbox is gone on return."""
        # Read once for the tiers core owns, so the agent's environment and
        # #29's per-run one come from one reading of the host, however long
        # the sandbox takes to start. A backend reads once for its own tier,
        # inside start(): the protocol hands it literals, not this (ADR-0034).
        host_env = dict(os.environ)
        # start() is an async context manager. ``entering`` is not used:
        # this orchestrator holds "a teardown failure never masks the result"
        # (ADR-0016), which is policy the stage runner leaves to it, so the
        # two halves are paired here instead (ADR-0032).
        cm = self.backend.start(workspace, env={})
        record.stage = "sandbox"
        try:
            sandbox = await run.stage("sandbox", cm.__aenter__())
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
            run.surface()  # held while the sandbox started
            state.sandbox = sandbox
            record.log.on_sandbox_ready(ctx)
            await self.hook_registry.fire("sandbox_ready", "sandbox", ctx)
            try:
                outcome = await self._run_agent(run, ctx, record, sandbox, host_env)
            except asyncio.CancelledError:
                # The exec killed the agent's tree before this was raised
                # (ADR-0023), and the runner is holding the cancellation, so
                # what the agent left is collected like any stopped agent's.
                outcome = None
            await self._collect(run, record, sandbox, workspace)
            return outcome
        finally:
            state.sandbox = None
            await self._teardown(run, cm, record)

    async def _run_agent(
        self,
        run: StageRunner,
        ctx: RunContext,
        record: _RunRecord,
        sandbox: Sandbox,
        host_env: Mapping[str, str],
    ) -> OutcomeT | None:
        """Agent stage: exec the provider's command, then ``agent_end``."""
        record.stage = "agent"
        prompt_text = ctx.prompt
        schema = TypeAdapter(self.outcome_type).json_schema()
        try:
            command = self.provider.command(prompt_text, schema)
        except Exception as exc:
            raise StageError("agent", Errored(exception=exc)) from exc

        record.log.agent_start(prompt_text)

        async def _on_output(line: AgentLine) -> None:
            record.log.on_agent_output(ctx, line)
            await self.hook_registry.fire("agent_output", "agent", ctx, line)

        outcome: OutcomeT | None = None
        try:
            record.agent, outcome = await run.stage(
                "agent",
                run_agent(
                    sandbox,
                    self.provider,
                    command,
                    self.outcome_type,
                    timeouts=self.bounds,
                    on_output=_on_output,
                    host_env=host_env,
                ),
                # run_agent owns silence, wall and completion grace, and a
                # cancellation is meant to stop an agent, not wait for one.
                bound=None,
                interruptible=True,
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
        self,
        run: StageRunner,
        record: _RunRecord,
        sandbox: Sandbox,
        workspace: Workspace,
    ) -> None:
        """Collect stage: best-effort once the run has already failed (ADR-0024)."""
        record.stage = "collect"
        try:
            # ``anyway``: a run cancelled mid-agent still collects what the
            # agent committed, which is the work its preservation branch keeps.
            series = await run.anyway(
                "collect", collect(sandbox, workspace, salvage=self.salvaging)
            )
            record.collected(series)
        except Exception as exc:
            err = _as_stage_error("collect", exc)
            # A stage hands over what it made before it failed, and collect
            # cut a series before it refused it: that is what preservation
            # keeps, whether or not this failure is the one reported.
            if err.series is not None:
                record.collected(err.series)
            record.fail(err)

    async def _land(
        self, run: StageRunner, ctx: RunContext, record: _RunRecord, outcome: OutcomeT
    ) -> RunResult[OutcomeT]:
        """Integrate stage, or preservation when the run integrates nowhere."""
        patches, strategy = record.patches, self.integration
        assert patches is not None
        if strategy is None:
            # No integrate stage runs, so the run stays where it ended, at
            # collect; preservation below names "integrate" for itself.
            await self._preserve(run, record)
            run.surface()  # held while the series was kept
            return (
                record.failed() if record.failure else record.succeeded(outcome, None)
            )

        record.stage = "integrate"

        report: IntegrationReport | None = None
        try:
            report = await run.stage(
                "integrate", integrate(self.repo, patches, strategy)
            )
        except Exception as exc:
            record.fail(_as_stage_error("integrate", exc))
        if record.failure is not None or report is None or report.conflict is not None:
            # Nothing landed, so the series is kept like any other that
            # reached no target (ADR-0005).
            await self._preserve(run, record)
        else:
            record.landed_on = report.target
        # The stage was atomic: a cancellation held meanwhile surfaces only
        # now, with the target moved or left alone, never half-moved.
        run.surface()
        if record.failure is not None:
            return record.failed()
        assert report is not None
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

    async def _preserve(self, run: StageRunner, record: _RunRecord) -> None:
        """Keep a series that reached no target on ``waystation/<run-id>``.

        Unbounded, and run ``anyway``: preservation is how a cancelled or
        failed run loses nothing (ADR-0016, ADR-0017).
        """
        if record.patches is None or record.patches.commits == 0:
            return
        branch = f"waystation/{record.run_id}"
        try:
            record.preserved = await run.anyway(
                "integrate",
                preserve_series(self.repo, branch=branch, series=record.patches),
                bound=None,
            )
        except Exception as exc:
            record.fail(_as_stage_error("integrate", exc))

    async def _teardown(
        self,
        run: StageRunner,
        cm: AbstractAsyncContextManager[Sandbox],
        record: _RunRecord,
    ) -> None:
        """Leave the sandbox; a failure is logged, never reported (ADR-0016).

        ``anyway``, so a cancelled run still tears its sandbox down, and
        bounded by ``teardown`` while staying the sandbox stage.
        """
        try:
            await run.anyway(
                "sandbox", cm.__aexit__(None, None, None), bound="teardown"
            )
        except Exception as exc:
            _log_later_failure(record.run_id, _as_stage_error("sandbox", exc))
