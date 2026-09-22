"""Flow authoring and the run orchestrator."""

from __future__ import annotations

import asyncio
import os
import secrets
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, overload

from waystation._outcome import outcome_schema
from waystation._run_record import RunRecord, log_later_failure
from waystation.agents.protocol import AgentLine, AgentProvider
from waystation.agents.run_agent import run_agent
from waystation.collect import collect
from waystation.errors import StageError
from waystation.hooks import HookEntry, HookName, HookRegistry, RunContext
from waystation.integration import (
    Integration,
    IntegrationStrategy,
    integrate,
    preserve_series,
)
from waystation.observability import bind_run
from waystation.preflight import preflight
from waystation.results import (
    AgentExit,
    Errored,
    IntegrationReport,
    RunFailed,
    RunResult,
    Stage,
    Summary,
    Timeouts,
)
from waystation.sandbox import allowlisted_env
from waystation.sandbox.protocol import Sandbox, SandboxBackend
from waystation.stages import StageRunner, stages
from waystation.workspace import Workspace, prepare_workspace

__all__ = ["Flow", "RunSpec"]


def _as_stage_error(stage: Stage, exc: Exception) -> StageError:
    """``exc`` as a failure of ``stage``, leaving a stage it already names alone.

    It claims the stage here rather than leaving it to ``fail``, because the
    teardown path logs its failure without ever reaching a record.
    """
    if isinstance(exc, StageError):
        return exc.at(stage)
    return StageError(stage, Errored(exc))


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

        Args:
            fn: The hook, called with the run's ``RunContext``.

        Returns:
            ``fn`` unchanged, so this works as a decorator. Runs built
            afterwards get it; a spec that already exists does not
            (ADR-0008).
        """
        return self._register("run_start", fn)

    def on_workspace_ready[F: Callable[[RunContext], object]](self, fn: F) -> F:
        """Fire ``fn(ctx)`` once the workspace is prepared.

        Args:
            fn: The hook, called with the run's ``RunContext``.

        Returns:
            ``fn`` unchanged, so this works as a decorator. Runs built
            afterwards get it; a spec that already exists does not
            (ADR-0008).
        """
        return self._register("workspace_ready", fn)

    def on_sandbox_ready[F: Callable[[RunContext], object]](self, fn: F) -> F:
        """Fire ``fn(ctx)`` once the sandbox is up, before the agent.

        Args:
            fn: The hook, called with the run's ``RunContext``.

        Returns:
            ``fn`` unchanged, so this works as a decorator. Runs built
            afterwards get it; a spec that already exists does not
            (ADR-0008).
        """
        return self._register("sandbox_ready", fn)

    def on_agent_output[F: Callable[[RunContext, AgentLine], object]](self, fn: F) -> F:
        """Fire ``fn(ctx, line)`` for each line the agent emits.

        Args:
            fn: The hook, called with the run's ``RunContext`` and the line.

        Returns:
            ``fn`` unchanged, so this works as a decorator. Runs built
            afterwards get it; a spec that already exists does not
            (ADR-0008).
        """
        return self._register("agent_output", fn)

    def on_agent_end[F: Callable[[RunContext, AgentExit], object]](self, fn: F) -> F:
        """Fire ``fn(ctx, exit)`` when the agent exec ends.

        Args:
            fn: The hook, called with the run's ``RunContext`` and the
                agent's ``AgentExit``.

        Returns:
            ``fn`` unchanged, so this works as a decorator. Runs built
            afterwards get it; a spec that already exists does not
            (ADR-0008).
        """
        return self._register("agent_end", fn)

    def on_integrated[F: Callable[[RunContext, IntegrationReport], object]](
        self, fn: F
    ) -> F:
        """Fire ``fn(ctx, report)`` when integration lands.

        Args:
            fn: The hook, called with the run's ``RunContext`` and the
                ``IntegrationReport``.

        Returns:
            ``fn`` unchanged, so this works as a decorator. Runs built
            afterwards get it; a spec that already exists does not
            (ADR-0008).
        """
        return self._register("integrated", fn)

    def on_run_end[F: Callable[[RunContext, RunResult[Any]], object]](self, fn: F) -> F:
        """Fire ``fn(ctx, result)`` for every result a run returns.

        Args:
            fn: The hook, called with the run's ``RunContext`` and its
                ``RunResult``.

        Returns:
            ``fn`` unchanged, so this works as a decorator. Runs built
            afterwards get it; a spec that already exists does not
            (ADR-0008).
        """
        return self._register("run_end", fn)

    def _register[F: Callable[..., object]](self, hook: HookName, fn: F) -> F:
        self._hook_entries.append(HookEntry(hook, fn))
        return fn

    # Overloaded, so a run that names no Outcome is a ``RunSpec[Summary]`` to
    # a type checker rather than one whose Outcome it cannot infer.
    @overload
    def run(self, prompt: str | Path) -> RunSpec[Summary]: ...

    @overload
    def run[OutcomeT](
        self, prompt: str | Path, *, outcome: type[OutcomeT]
    ) -> RunSpec[OutcomeT]: ...

    def run(self, prompt: str | Path, *, outcome: type[Any] = Summary) -> RunSpec[Any]:
        """Describe a run of this flow's agent; awaiting the spec performs it.

        Nothing runs here. The spec carries the flow's defaults and a snapshot
        of its hooks, and its builders change them for this run alone
        (ADR-0008, ADR-0022).

        Args:
            prompt: The instructions handed to the agent, or a path to a file
                holding them, read once as each run starts (ADR-0045).
            outcome: The object-shaped type the agent reports back — a
                ``BaseModel``, dataclass or ``TypedDict``. ``Summary`` when
                none is named.

        Returns:
            A ``RunSpec`` for the run.

        Raises:
            TypeError: When ``outcome`` has no JSON schema or is not
                object-shaped; refused here, before anything runs
                (ADR-0038).
        """
        outcome_schema(outcome)  # refused here as in run_agent (ADR-0038)
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
        prompt: The instructions handed to the agent, or a path to them,
            read once as each run starts (ADR-0045).
        outcome_type: The object-shaped type the agent reports back.
        bounds: The per-stage ``Timeouts`` — ``.timeouts()`` sets them.
        salvaging: Whether work left uncommitted is salvaged —
            ``.salvage()`` sets it.
        integration: Where the series lands, or ``None`` to land nowhere —
            ``.integrate()`` sets it.
        hook_registry: The hooks that fire — ``.hooks()`` and ``.on_<hook>()``
            append to it.
        environment: The per-run tier's literal values — ``.env()`` sets
            them.
        pass_through: The per-run tier's host variables — ``.pass_env()``
            sets them.
        label: The name the run's results and console carry, or ``None`` —
            ``.name()`` sets it.
        travelling_refs: The host refs the workspace carries besides its own
            branch — ``.extra_refs()`` sets them.
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
    environment: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    pass_through: tuple[str, ...] = ()
    label: str | None = None
    travelling_refs: tuple[str, ...] = ()

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

    def env(self, values: Mapping[str, str]) -> RunSpec[OutcomeT]:
        """Replace this run's literal environment values.

        The per-run tier is the most specific of the three, so it wins over
        the sandbox spec's and the agent provider's (ADR-0013), and it goes
        in when the sandbox starts, so every exec of the run sees it — collect
        included, unlike a provider's (ADR-0034). Within the tier a literal
        beats a ``.pass_env()`` name. ``GIT_AUTHOR_NAME`` and
        ``GIT_AUTHOR_EMAIL`` here author the agent's commits instead of the
        identity the workspace copied from the host.

        Args:
            values: The variables and values to set. Replaces what an earlier
                ``.env()`` set rather than merging with it.

        Returns:
            A new spec; this one is unchanged.
        """
        # A copy behind a read-only view: the spec is frozen, so its mapping
        # is too, and the caller's is theirs to go on changing (ADR-0022).
        return replace(self, environment=MappingProxyType(dict(values)))

    def pass_env(self, *names: str) -> RunSpec[OutcomeT]:
        """Replace the host variables this run passes through.

        Each is read from the host when the run starts, not now, and one the
        host lacks is skipped (ADR-0034). They rank with ``.env()``: over the
        sandbox spec and the agent provider, into every exec of the run.

        Args:
            *names: The variables to pass through. Replaces what an earlier
                ``.pass_env()`` named.

        Returns:
            A new spec; this one is unchanged.
        """
        return replace(self, pass_through=names)

    def name(self, label: str) -> RunSpec[OutcomeT]:
        """Name this run, for the results it returns and the console.

        Args:
            label: What to call it. The run id is still made and still
                carried; a name only stands beside it.

        Returns:
            A new spec; this one is unchanged.
        """
        return replace(self, label=label)

    def extra_refs(self, *refs: str) -> RunSpec[OutcomeT]:
        """Make these host refs travel into the workspace, under the same names.

        A workspace otherwise carries its own branch and nothing else of the
        host's (ADR-0037). A resolver run names the preservation branch here,
        so its agent can cherry-pick from it (ADR-0015). Each is resolved when
        the workspace stage starts, and one that names no branch or tag on
        the host fails the run there: ``Refused("missing_extra_ref")``.

        Args:
            *refs: Branch or tag names on the host. Replaces what an earlier
                ``.extra_refs()`` named.

        Returns:
            A new spec; this one is unchanged.
        """
        return replace(self, travelling_refs=refs)

    # Per-run hooks: each returns a new RunSpec whose hooks fire after the
    # flow's. Bundles are any objects with a subset of the ``on_<hook>`` methods.

    def hooks(self, *bundles: object) -> RunSpec[OutcomeT]:
        """Add each bundle's ``on_<hook>`` methods to this run's hooks.

        Args:
            *bundles: Any objects with a subset of the ``on_<hook>`` methods,
                a ``HookBundle`` or not. Their hooks fire after the flow's.

        Returns:
            A new spec; this one is unchanged.

        Raises:
            TypeError: When a bundle defines none of the ``on_<hook>`` methods.
        """
        return replace(self, hook_registry=self.hook_registry.with_bundles(*bundles))

    def on_run_start(self, fn: Callable[[RunContext], object]) -> RunSpec[OutcomeT]:
        """Fire ``fn(ctx)`` as a run starts.

        Args:
            fn: The hook, called with the run's ``RunContext``. It fires
                after the flow's hooks for the same event.

        Returns:
            A new spec; this one is unchanged.
        """
        return self._with_hook("run_start", fn)

    def on_workspace_ready(
        self, fn: Callable[[RunContext], object]
    ) -> RunSpec[OutcomeT]:
        """Fire ``fn(ctx)`` once the workspace is prepared.

        Args:
            fn: The hook, called with the run's ``RunContext``. It fires
                after the flow's hooks for the same event.

        Returns:
            A new spec; this one is unchanged.
        """
        return self._with_hook("workspace_ready", fn)

    def on_sandbox_ready(self, fn: Callable[[RunContext], object]) -> RunSpec[OutcomeT]:
        """Fire ``fn(ctx)`` once the sandbox is up, before the agent.

        Args:
            fn: The hook, called with the run's ``RunContext``. It fires
                after the flow's hooks for the same event.

        Returns:
            A new spec; this one is unchanged.
        """
        return self._with_hook("sandbox_ready", fn)

    def on_agent_output(
        self, fn: Callable[[RunContext, AgentLine], object]
    ) -> RunSpec[OutcomeT]:
        """Fire ``fn(ctx, line)`` for each line the agent emits.

        Args:
            fn: The hook, called with the run's ``RunContext`` and the line.
                It fires after the flow's hooks for the same event.

        Returns:
            A new spec; this one is unchanged.
        """
        return self._with_hook("agent_output", fn)

    def on_agent_end(
        self, fn: Callable[[RunContext, AgentExit], object]
    ) -> RunSpec[OutcomeT]:
        """Fire ``fn(ctx, exit)`` when the agent exec ends.

        Args:
            fn: The hook, called with the run's ``RunContext`` and the
                agent's ``AgentExit``. It fires after the flow's hooks for
                the same event.

        Returns:
            A new spec; this one is unchanged.
        """
        return self._with_hook("agent_end", fn)

    def on_integrated(
        self, fn: Callable[[RunContext, IntegrationReport], object]
    ) -> RunSpec[OutcomeT]:
        """Fire ``fn(ctx, report)`` when integration lands.

        Args:
            fn: The hook, called with the run's ``RunContext`` and the
                ``IntegrationReport``. It fires after the flow's hooks for
                the same event.

        Returns:
            A new spec; this one is unchanged.
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

        Args:
            fn: The hook, called with the run's ``RunContext`` and its
                ``RunResult``. It fires after the flow's hooks for the same
                event.

        Returns:
            A new spec; this one is unchanged.
        """
        return self._with_hook("run_end", fn)

    def _with_hook(
        self, hook: HookName, fn: Callable[..., object]
    ) -> RunSpec[OutcomeT]:
        return replace(self, hook_registry=self.hook_registry.with_function(hook, fn))

    def __await__(self) -> Generator[Any, None, RunResult[OutcomeT]]:
        # Typed, so ``await spec`` is a ``RunResult`` a ``match`` can be
        # checked for exhaustiveness against, not ``Any``.
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
        record = RunRecord(run_id=secrets.token_hex(4), name=self.label, repo=self.repo)
        ctx = RunContext(record)
        with bind_run(record.run_id, record.name):
            # Bound, so what preflight logs carries the id, but a run that
            # fails it never began: no hook fires and no workspace is made.
            # A lone run is a batch of one, checked the way fan-out checks
            # a batch (#30).
            if not preflighted:
                await preflight((self,))
            record.log.on_run_start(ctx)
            try:
                result = await self._staged(ctx, record)
                if isinstance(result, RunFailed):
                    # Owed work went on after the failure — collect after the
                    # agent, say — but the run ended where it failed, and
                    # run_end's hooks read that off ctx (ADR-0039).
                    record.stage = result.stage
                try:
                    # Every run_end hook sees the result, even after one raises.
                    await self.hook_registry.fire(
                        "run_end", ctx, result, stop_on_raise=False
                    )
                except StageError as err:
                    record.fail(err)
                    result = record.failed()
            except asyncio.CancelledError:
                # Logged, never reported: there is no Cancelled result (ADR-0017).
                record.log_cancelled()
                raise
            finally:
                # ctx outlives the run in whatever an observer kept — a
                # Dashboard row does — and the diffs are the heavy part of
                # what it would hold: nothing reads them once the run is over.
                record.patches = None
            record.log.on_run_end(ctx, result)
            return result

    async def _staged(self, ctx: RunContext, record: RunRecord) -> RunResult[OutcomeT]:
        """Run the lifecycle through one stage runner, which holds the guarantees.

        Every bound, every held cancellation and every elapsed time below
        comes from ``run``; what is left here is this orchestrator's policy
        (ADR-0032).
        """
        async with stages(self.bounds) as run:
            record.elapsed = run.elapsed
            try:
                return await self._lifecycle(run, ctx, record)
            finally:
                record.cancelled_during = run.cancelled_during

    async def _lifecycle(
        self,
        run: StageRunner,
        ctx: RunContext,
        record: RunRecord,
    ) -> RunResult[OutcomeT]:
        """Run the stages in order; each phase below owns its own stages."""
        try:
            # Read once, before run_start, so every hook and the agent see the
            # same text (ADR-0045). run_start fires for every run, so a prompt
            # file that cannot be read waits its turn: the run started, and
            # then it failed.
            prompt_error: StageError | None = None
            try:
                record.prompt = self._prompt_text()
            except StageError as err:
                prompt_error = err
            await self.hook_registry.fire("run_start", ctx)
            if prompt_error is not None:
                raise prompt_error
            workspace = await self._prepare(run, record)
            try:
                await self._workspace_ready(run, ctx, record)
                outcome = await self._in_sandbox(run, ctx, record, workspace)
                if record.failure is not None or run.cancelled_during is not None:
                    await self._preserve(run, record)
                    run.surface()  # before integrate starts: it never will
                    return record.failed()
                assert outcome is not None
                return await self._land(run, ctx, record, outcome)
            finally:
                # The workspace's whole life, and its one removal. ``anyway``
                # so a cancelled run still cleans up, and unbounded because
                # nothing asked for a limit on it (ADR-0017, ADR-0032).
                await run.anyway("workspace", workspace.remove(), bound=None)
        except Exception as exc:
            record.fail(_as_stage_error(record.stage, exc))
        run.surface()  # a failure never hides a cancellation held meanwhile
        return record.failed()

    def _prompt_text(self) -> str:
        """The prompt as text, whether the flow script gave a string or a path.

        Resolved once, before ``run_start``, so every hook sees ``ctx.prompt``
        and the agent is handed that same text (ADR-0045). A file that cannot
        be read still fails the agent stage: preparing the agent's prompt is
        the agent's business, whenever it happens.
        """
        if not isinstance(self.prompt, Path):
            return self.prompt
        try:
            return self.prompt.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise StageError("agent", Errored(exception=exc)) from exc

    async def _prepare(self, run: StageRunner, record: RunRecord) -> Workspace:
        """Workspace stage: a private clone of the base ref.

        Announcing it is ``_workspace_ready``, a step later, so that the
        whole of the workspace's life — hook included — sits inside the one
        block that removes it (#76).
        """
        record.stage = "workspace"
        workspace = await run.stage(
            "workspace",
            prepare_workspace(
                self.repo,
                base=self.base_ref,
                run_id=record.run_id,
                extra_refs=self.travelling_refs,
            ),
        )
        record.base_sha = workspace.base_sha
        record.branch = workspace.branch
        return workspace

    async def _workspace_ready(
        self, run: StageRunner, ctx: RunContext, record: RunRecord
    ) -> None:
        """Tell the observers there is a workspace, and fire the hook."""
        run.surface()  # held while the workspace was cloned
        record.log.on_workspace_ready(ctx)
        await self.hook_registry.fire("workspace_ready", ctx)

    async def _in_sandbox(
        self,
        run: StageRunner,
        ctx: RunContext,
        record: RunRecord,
        workspace: Workspace,
    ) -> OutcomeT | None:
        """Sandbox, agent and collect stages; the sandbox is gone on return."""
        # Read once for the tiers core owns, so the agent's environment and
        # the per-run one come from one reading of the host, however long
        # the sandbox takes to start. A backend reads once for its own tier,
        # inside start(): the protocol hands it literals, not this (ADR-0034).
        host_env = dict(os.environ)
        # Handed to start(), so it reaches every exec over the spec's tier,
        # and to the agent exec again, over the provider's (ADR-0013).
        per_run = allowlisted_env(
            literal=self.environment, pass_env=self.pass_through, host_env=host_env
        )
        # start() is an async context manager. ``entering`` is not used:
        # this orchestrator holds "a teardown failure never masks the result"
        # (ADR-0016), which is policy the stage runner leaves to it, so the
        # two halves are paired here instead (ADR-0032).
        cm = self.backend.start(workspace, env=per_run)
        record.stage = "sandbox"
        # A start that failed cleans up after itself: ``async with`` never
        # exits a context it did not enter. The workspace is not part of
        # that — core removes it whether or not a sandbox ever started (#76).
        sandbox = await run.stage("sandbox", cm.__aenter__())
        try:
            run.surface()  # held while the sandbox started
            record.sandbox = sandbox
            record.log.on_sandbox_ready(ctx)
            await self.hook_registry.fire("sandbox_ready", ctx)
            outcome = await self._run_agent(
                run, ctx, record, sandbox, host_env, per_run
            )
            await self._collect(run, record, sandbox, workspace)
            return outcome
        finally:
            record.sandbox = None
            await self._teardown(run, cm, record)

    async def _run_agent(
        self,
        run: StageRunner,
        ctx: RunContext,
        record: RunRecord,
        sandbox: Sandbox,
        host_env: Mapping[str, str],
        per_run: Mapping[str, str],
    ) -> OutcomeT | None:
        """Agent stage: exec the provider's command, then ``agent_end``."""
        record.stage = "agent"

        async def _on_output(line: AgentLine) -> None:
            record.log.on_agent_output(ctx, line)
            await self.hook_registry.fire("agent_output", ctx, line)

        # Read as the run started, not now: the agent gets what ctx showed
        # every hook before it (ADR-0045).
        prompt_text = ctx.prompt
        # Built before agent_start: a provider that cannot build its command
        # never started an agent, so no agent_end fires for it either
        # (ADR-0038).
        try:
            agent_run = run_agent(
                sandbox,
                self.provider,
                prompt_text,
                self.outcome_type,
                timeouts=self.bounds,
                on_output=_on_output,
                host_env=host_env,
                env=per_run,
            )
        except Exception as exc:
            raise StageError("agent", Errored(exception=exc)) from exc

        record.log.agent_start(prompt_text)

        outcome: OutcomeT | None = None
        try:
            record.agent, outcome = await run.stage(
                "agent",
                agent_run,
                # run_agent owns silence, wall and completion grace, and a
                # cancellation is meant to stop an agent, not wait for one.
                bound=None,
                interruptible=True,
            )
        except asyncio.CancelledError:
            # The exec killed the agent's tree before this was raised
            # (ADR-0023), and the runner is holding the cancellation. agent_end
            # still fires, with the sandbox up: it is the one point a cancelled
            # run can carry a file out before teardown (#154). What the agent
            # left is then collected like any stopped agent's.
            record.agent = AgentExit(
                exit_code=-1,
                elapsed=record.elapsed["agent"],
                hanging=False,
                cancelled=True,
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
            await self.hook_registry.fire("agent_end", ctx, record.agent)
        except StageError as err:
            record.fail(err)
        return outcome

    async def _collect(
        self,
        run: StageRunner,
        record: RunRecord,
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
        self, run: StageRunner, ctx: RunContext, record: RunRecord, outcome: OutcomeT
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
            await self.hook_registry.fire("integrated", ctx, report)
        except StageError as err:
            # The series landed; a hook failure must not preserve it.
            record.fail(err)
            return record.failed()
        return record.succeeded(outcome, report)

    async def _preserve(self, run: StageRunner, record: RunRecord) -> None:
        """Keep a series that reached no target on ``waystation/<run-id>``.

        Unbounded, and run ``anyway``: preservation is how a cancelled or
        failed run loses nothing (ADR-0016, ADR-0017).
        """
        # No branch means no workspace, and so nothing was ever collected;
        # naming it keeps the type honest rather than asserting.
        if record.patches is None or record.patches.commits == 0:
            return
        if (branch := record.branch) is None:
            return
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
        record: RunRecord,
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
            log_later_failure(record.run_id, _as_stage_error("sandbox", exc))
