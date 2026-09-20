"""A run spec's builders own the bare names; its values take the glossary's.

The collision was deciding the API one ticket at a time — `RunSpec` is a
frozen dataclass, so its fields took `agent`, `sandbox`, `base`, `timeouts`
and `salvage` first, and `.timeouts(t)` had to ship as `with_timeouts` to
get out of the way (#26). ADR-0031 settles it the other way round, and the
first test below is what stops it coming back.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import BaseModel

from waystation import (
    Flow,
    Integration,
    NoSandbox,
    RunSpec,
    ScriptedAgent,
    Timeouts,
)


class Answer(BaseModel):
    summary: str


def _spec() -> RunSpec[Answer]:
    """A spec built the only public way there is: from a flow."""
    return Flow(
        Path("repo"),
        agent=ScriptedAgent(outcome=Answer(summary="ok")),
        sandbox=NoSandbox(),
    ).run("work", outcome=Answer)


@pytest.mark.unit
def test_no_public_method_of_a_run_spec_is_also_a_field() -> None:
    """The rule ADR-0031 settled, held by a test so it cannot be re-decided."""
    fields = {field.name for field in dataclasses.fields(RunSpec)}
    methods = {
        name
        for name, value in vars(RunSpec).items()
        if not name.startswith("_") and callable(value)
    }

    collisions = sorted(fields & methods)
    assert not collisions, (
        f"these name both a builder and a field: {collisions}. Builders own "
        "the bare names; a stored value takes the word CONTEXT.md uses for it "
        "(ADR-0031)."
    )


@pytest.mark.unit
def test_a_spec_stores_its_values_under_the_glossarys_words() -> None:
    spec = _spec()

    assert isinstance(spec.provider, ScriptedAgent)
    assert isinstance(spec.backend, NoSandbox)
    assert spec.base_ref == "HEAD"
    assert spec.bounds == Timeouts()
    assert spec.salvaging is True
    assert spec.integration is None


_OTHER_AGENT = ScriptedAgent(outcome=Answer(summary="different"))
_OTHER_SANDBOX = NoSandbox(env={"WAYSTATION": "1"})


@pytest.mark.unit
@pytest.mark.parametrize(
    ("build", "reads", "expected"),
    [
        (lambda s: s.agent(_OTHER_AGENT), "provider", _OTHER_AGENT),
        (lambda s: s.sandbox(_OTHER_SANDBOX), "backend", _OTHER_SANDBOX),
        (lambda s: s.base("main"), "base_ref", "main"),
        (lambda s: s.timeouts(Timeouts(collect=9.0)), "bounds", Timeouts(collect=9.0)),
        (lambda s: s.salvage(False), "salvaging", False),
        (lambda s: s.integrate("feature"), "integration", Integration("feature")),
    ],
    ids=["agent", "sandbox", "base", "timeouts", "salvage", "integrate"],
)
def test_a_builder_returns_a_new_spec_and_leaves_the_old_one_alone(
    build: Callable[[RunSpec[Answer]], RunSpec[Answer]], reads: str, expected: object
) -> None:
    """Every builder is generative: the spec you started from is unchanged."""
    spec = _spec()
    before = getattr(spec, reads)

    built = build(spec)

    assert built is not spec
    assert getattr(built, reads) == expected
    assert getattr(spec, reads) == before, "the original spec was mutated"
    assert before != expected, "this case proves nothing: it set what was there"


@pytest.mark.unit
def test_a_builder_replaces_its_value_rather_than_merging_it() -> None:
    """``dataclasses.replace`` semantics: the whole ``Timeouts`` is swapped."""
    spec = _spec().timeouts(Timeouts(collect=9.0, integrate=3.0))

    replaced = spec.timeouts(Timeouts(collect=1.0))

    assert replaced.bounds == Timeouts(collect=1.0)
    assert replaced.bounds.integrate is None, "the old value survived a replace"


@pytest.mark.unit
def test_a_spec_is_read_only() -> None:
    spec = _spec()

    with pytest.raises(dataclasses.FrozenInstanceError):
        spec.base_ref = "main"  # type: ignore[misc]


@pytest.mark.unit
def test_the_builders_chain_in_any_order() -> None:
    spec = (
        _spec()
        .base("main")
        .salvage(False)
        .timeouts(Timeouts(agent_wall=30.0))
        .integrate("agents/x", mechanism="merge")
    )

    assert spec.base_ref == "main"
    assert spec.salvaging is False
    assert spec.bounds.agent_wall == 30.0
    assert spec.integration == Integration("agents/x", mechanism="merge")
