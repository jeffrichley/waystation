"""The Outcome contract comes from one place: the type a run validates against.

``run_agent`` derives the schema the provider is handed from ``outcome_type``
itself, so the agent can never be asked for one shape and judged on another
(ADR-0019, #80). It builds the command when called, not when awaited, so a
refused type or a provider that cannot build its command fails at the call.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any, Literal

import pytest
from pydantic import BaseModel, Field, TypeAdapter
from pydantic.errors import PydanticUserError

from helpers import RecordingAgent
from waystation import Flow, NoSandbox, run_agent
from waystation.agents import AgentCommand
from waystation.sandbox import Sandbox


class Answer(BaseModel):
    summary: str


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


class CallableField(BaseModel):
    on_done: Callable[[], None]


def _early_bound() -> type[BaseModel]:
    """A model naming one defined after it, in a scope its schema can't see."""

    class Early(BaseModel):
        later: Later

    class Later(BaseModel):
        summary: str

    return Early


@pytest.mark.unit
@pytest.mark.parametrize(
    ("outcome_type", "name"),
    [(CallableField, "CallableField"), (_early_bound(), "Early")],
    ids=["field-with-no-schema", "unresolvable-name"],
)
def test_run_agent_refuses_a_schemaless_outcome_before_the_provider_is_asked(
    outcome_type: type[BaseModel], name: str
) -> None:
    agent = RecordingAgent()

    with pytest.raises(TypeError, match=rf"{name}.*BaseModel, dataclass or TypedDict"):
        _ = run_agent(_NO_SANDBOX, agent, "report back", outcome_type)

    assert agent.schemas == []


class Left(BaseModel):
    side: Literal["left"]


class Right(BaseModel):
    side: Literal["right"]


@pytest.mark.unit
def test_misusing_pydantic_is_not_disguised_as_a_refused_outcome() -> None:
    """A discriminator naming no field is a bug in the type, not its shape."""
    misused = Annotated[Left | Right, Field(discriminator="nope")]

    with pytest.raises(PydanticUserError, match="nope"):
        _ = run_agent(_NO_SANDBOX, RecordingAgent(), "report back", misused)  # type: ignore[arg-type]


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
