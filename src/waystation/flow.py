"""Flow authoring and the run orchestrator."""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter
from pydantic.json_schema import GenerateJsonSchema

from waystation.agents.protocol import AgentProvider
from waystation.agents.run_agent import run_agent
from waystation.errors import PreflightError, StageError
from waystation.results import (
    AgentExit,
    Errored,
    Failure,
    RunFailed,
    RunSucceeded,
    Stage,
    Summary,
    Timeouts,
)
from waystation.sandbox.protocol import SandboxBackend
from waystation.workspace import prepare_workspace


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
) -> RunFailed:
    return RunFailed(
        run_id=run_id,
        name=None,
        base_sha=base_sha,
        elapsed=elapsed,
        agent=agent,
        series=None,
        preserved=None,
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
    integration: None = field(default=None, kw_only=True)
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

    def __await__(self):  # type: ignore[no-untyped-def]
        return self._execute().__await__()

    async def _execute(self) -> RunSucceeded[OutcomeT] | RunFailed:
        run_id = ""
        base_sha: str | None = None
        elapsed: dict[Stage, float] = {}
        agent_exit: AgentExit | None = None
        stage: Stage = "workspace"

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
            workspace = prepare_workspace(self.repo, base=self.base)
            elapsed["workspace"] = time.perf_counter() - t0
            run_id = workspace.run_id
            base_sha = workspace.base_sha

            stage = "sandbox"
            t1 = time.perf_counter()
            async with self.sandbox.start(workspace, env={}, pass_env=()) as sandbox:
                elapsed["sandbox"] = time.perf_counter() - t1

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

                t2 = time.perf_counter()
                try:
                    agent_exit, outcome = await run_agent(
                        sandbox,
                        self.agent,
                        command,
                        self.outcome_type,
                    )
                except StageError:
                    elapsed["agent"] = time.perf_counter() - t2
                    raise
                except Exception as exc:
                    elapsed["agent"] = time.perf_counter() - t2
                    raise StageError("agent", Errored(exception=exc)) from exc
                elapsed["agent"] = time.perf_counter() - t2

            return RunSucceeded(
                run_id=run_id,
                name=None,
                base_sha=base_sha,
                elapsed=elapsed,
                agent=agent_exit,
                series=None,
                preserved=None,
                outcome=outcome,
                report=None,
            )
        except StageError as err:
            return _run_failed(
                run_id=run_id,
                base_sha=base_sha,
                elapsed=elapsed,
                agent=err.agent if err.agent is not None else agent_exit,
                stage=err.stage,
                failure=err.failure,
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
            )
        except Exception as exc:
            return _run_failed(
                run_id=run_id,
                base_sha=base_sha,
                elapsed=elapsed,
                agent=agent_exit,
                stage=stage,
                failure=Errored(exception=exc),
            )
