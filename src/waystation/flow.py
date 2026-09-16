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
from waystation.results import RunSucceeded, Summary, Timeouts
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


@dataclass(frozen=True, slots=True)
class Flow:
    """Defaults shared by runs against one host repo."""

    repo: Path | str
    agent: AgentProvider
    sandbox: SandboxBackend
    base: str = "HEAD"
    integration: None = None
    timeouts: Timeouts = field(default_factory=Timeouts)
    salvage: bool = True
    hooks: Sequence[Any] = ()

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

    async def _execute(self) -> RunSucceeded[OutcomeT]:
        self.agent.preflight()
        await self.sandbox.preflight()

        elapsed: dict[str, float] = {}

        t0 = time.perf_counter()
        workspace = prepare_workspace(self.repo, base=self.base)
        elapsed["workspace"] = time.perf_counter() - t0

        t1 = time.perf_counter()
        async with self.sandbox.start(workspace, env={}, pass_env=()) as sandbox:
            elapsed["sandbox"] = time.perf_counter() - t1

            if isinstance(self.prompt, Path):
                prompt_text = self.prompt.read_text(encoding="utf-8")
            else:
                prompt_text = self.prompt

            schema = TypeAdapter(self.outcome_type).json_schema()
            command = self.agent.command(prompt_text, schema)

            t2 = time.perf_counter()
            agent_exit, outcome = await run_agent(
                sandbox,
                self.agent,
                command,
                self.outcome_type,
            )
            elapsed["agent"] = time.perf_counter() - t2

        return RunSucceeded(
            run_id=workspace.run_id,
            name=None,
            base_sha=workspace.base_sha,
            elapsed=elapsed,  # type: ignore[arg-type]
            agent=agent_exit,
            series=None,
            preserved=None,
            outcome=outcome,
            report=None,
        )
