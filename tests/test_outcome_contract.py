"""The Outcome contract comes from one place: the type a run validates against.

``run_agent`` derives the schema the provider is handed from ``outcome_type``
itself, so the agent can never be asked for one shape and judged on another
(ADR-0019, #80). It builds the command when called, not when awaited, so a
refused type or a provider that cannot build its command fails at the call.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, TypeAdapter

from waystation import AgentCommand, Flow, NoSandbox, Sandbox, run_agent
from waystation.agents import AgentEvent


class Answer(BaseModel):
    summary: str


@dataclass
class RecordingAgent:
    """An agent that remembers every schema it was asked to deliver."""

    schemas: list[dict[str, Any]] = field(default_factory=list)

    def preflight(self) -> None:
        return None

    def command(self, prompt: str, outcome_schema: dict[str, Any]) -> AgentCommand:
        self.schemas.append(outcome_schema)
        return AgentCommand(argv=("true",))

    def parse(self, line: str) -> Sequence[AgentEvent]:
        return ()


# Never touched: every test here stops before the exec begins.
_NO_SANDBOX: Sandbox = None  # type: ignore[assignment]


@pytest.mark.unit
def test_the_provider_is_asked_for_the_schema_of_the_type_that_is_validated() -> None:
    agent = RecordingAgent()

    run_agent(_NO_SANDBOX, agent, "report back", Answer).close()

    assert agent.schemas == [TypeAdapter(Answer).json_schema()]


@pytest.mark.unit
def test_run_agent_refuses_a_non_object_outcome_before_the_provider_is_asked() -> None:
    agent = RecordingAgent()

    with pytest.raises(TypeError, match="object-shaped"):
        _ = run_agent(_NO_SANDBOX, agent, "report back", str)

    assert agent.schemas == []


class Hook(BaseModel):
    on_done: Callable[[], None]


@pytest.mark.unit
def test_run_agent_refuses_a_schemaless_outcome_before_the_provider_is_asked() -> None:
    agent = RecordingAgent()

    with pytest.raises(TypeError, match=r"Hook.*BaseModel, dataclass or TypedDict"):
        _ = run_agent(_NO_SANDBOX, agent, "report back", Hook)

    assert agent.schemas == []


@pytest.mark.unit
def test_a_provider_that_cannot_build_its_command_fails_at_the_call() -> None:
    class Boom(RecordingAgent):
        def command(self, prompt: str, outcome_schema: dict[str, Any]) -> AgentCommand:
            raise RuntimeError("no command")

    with pytest.raises(RuntimeError, match="no command"):
        _ = run_agent(_NO_SANDBOX, Boom(), "report back", Answer)


@pytest.mark.unit
def test_run_agent_and_flow_run_refuse_a_non_object_outcome_alike(
    tmp_path: Path,
) -> None:
    """One implementation behind both doors, so the two never drift apart."""
    with pytest.raises(TypeError) as by_primitive:
        _ = run_agent(_NO_SANDBOX, RecordingAgent(), "x", list[int])
    flow = Flow(tmp_path, agent=RecordingAgent(), sandbox=NoSandbox())
    with pytest.raises(TypeError) as by_flow:
        flow.run("x", outcome=list[int])

    assert str(by_primitive.value) == str(by_flow.value)
