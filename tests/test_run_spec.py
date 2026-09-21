"""A run spec's builders own the bare names; its values take the glossary's.

The argument is in ADR-0031; these hold it, so #29 and #32 inherit the rule
instead of re-deciding it when they add their own builders.
"""

from __future__ import annotations

import dataclasses
import inspect
from collections.abc import Callable
from pathlib import Path

import pytest

from helpers import ShellAgent, a_run
from waystation import (
    Integration,
    NoSandbox,
    RunFailed,
    RunSpec,
    RunSucceeded,
    ScriptedAgent,
    Summary,
    Timeouts,
)

# The bare names the builders own, spelled out rather than read off the class:
# a list derived from `RunSpec` would agree with whatever `RunSpec` became.
_BUILDERS = (
    "agent",
    "sandbox",
    "base",
    "salvage",
    "timeouts",
    "integrate",
    "hooks",
    "env",
    "pass_env",
    "name",
    "extra_refs",
)

_ANY_REPO = Path("repo")
"""No run is performed here, so a spec needs no repo that exists."""


@pytest.mark.unit
@pytest.mark.parametrize("builder", _BUILDERS)
def test_a_builders_name_is_not_reclaimed_by_a_field(builder: str) -> None:
    """A field taking a builder's name would shadow it, and silently.

    `RunSpec` is a slots dataclass, so a field wins a name outright: the
    attribute becomes a slot descriptor and the method is simply gone. That
    is what this catches.
    """
    attribute = getattr(RunSpec, builder, None)

    assert inspect.isfunction(attribute), (
        f"RunSpec.{builder} is {type(attribute).__name__}, not a method. A "
        "field has taken a builder's name; builders own the bare names, and a "
        "stored value takes the word CONTEXT.md uses for it (ADR-0031)."
    )


@pytest.mark.unit
def test_no_field_takes_a_name_a_builder_owns() -> None:
    fields = {field.name for field in dataclasses.fields(RunSpec)}

    taken = sorted(fields & set(_BUILDERS))
    assert not taken, (
        f"these fields take names the builders own: {taken}. Store the value "
        "under the glossary's word instead (ADR-0031)."
    )


@pytest.mark.unit
def test_a_spec_stores_its_values_under_the_glossary_s_words() -> None:
    agent = ScriptedAgent(outcome={"summary": "ok"})
    backend = NoSandbox()
    spec = a_run(_ANY_REPO).agent(agent).sandbox(backend)

    assert spec.provider is agent
    assert spec.backend is backend
    assert spec.base_ref == "HEAD"
    assert spec.bounds == Timeouts()
    assert spec.salvaging is True
    assert spec.integration is None
    assert spec.environment == {}
    assert spec.pass_through == ()
    assert spec.label is None
    assert spec.travelling_refs == ()


_OTHER_AGENT = ScriptedAgent(outcome={"summary": "different"})
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
        (lambda s: s.env({"WAYSTATION": "1"}), "environment", {"WAYSTATION": "1"}),
        (lambda s: s.pass_env("CI", "HOME"), "pass_through", ("CI", "HOME")),
        (lambda s: s.name("fix-the-flake"), "label", "fix-the-flake"),
        (
            lambda s: s.extra_refs("waystation/0a1b"),
            "travelling_refs",
            ("waystation/0a1b",),
        ),
    ],
    ids=[
        "agent",
        "sandbox",
        "base",
        "timeouts",
        "salvage",
        "integrate",
        "env",
        "pass_env",
        "name",
        "extra_refs",
    ],
)
def test_a_builder_returns_a_new_spec_and_leaves_the_old_one_alone(
    build: Callable[[RunSpec[Summary]], RunSpec[Summary]], reads: str, expected: object
) -> None:
    """Every builder is generative: the spec you started from is unchanged."""
    spec = a_run(_ANY_REPO)
    before = getattr(spec, reads)
    assert before != expected, "this case sets what was there, so proves nothing"

    built = build(spec)

    assert built is not spec
    assert getattr(built, reads) == expected
    assert getattr(spec, reads) == before, "the original spec was mutated"


@pytest.mark.unit
def test_a_builder_replaces_its_value_rather_than_merging_it() -> None:
    """``dataclasses.replace`` semantics: the whole ``Timeouts`` is swapped."""
    spec = a_run(_ANY_REPO).timeouts(Timeouts(collect=9.0, integrate=3.0))

    replaced = spec.timeouts(Timeouts(collect=1.0))

    assert replaced.bounds == Timeouts(collect=1.0)
    assert replaced.bounds.integrate is None, "the old value survived a replace"


@pytest.mark.unit
def test_the_environment_builders_replace_rather_than_merge_too() -> None:
    """``.env()`` names the whole per-run tier, as ``.timeouts()`` does its bounds."""
    spec = a_run(_ANY_REPO).env({"A": "1"}).pass_env("CI")

    replaced = spec.env({"B": "2"}).pass_env("HOME")

    assert replaced.environment == {"B": "2"}
    assert replaced.pass_through == ("HOME",)


@pytest.mark.unit
def test_extra_refs_replaces_rather_than_merges() -> None:
    """Like ``.pass_env()``: the refs named last are the refs that travel."""
    spec = a_run(_ANY_REPO).extra_refs("one", "two")

    assert spec.extra_refs("three").travelling_refs == ("three",)
    assert spec.extra_refs().travelling_refs == ()


@pytest.mark.unit
def test_a_spec_keeps_its_own_copy_of_the_environment() -> None:
    """A mapping changed after ``.env()`` must not reach a spec that is frozen."""
    given = {"A": "1"}
    spec = a_run(_ANY_REPO).env(given)

    given["A"] = "changed"

    assert spec.environment == {"A": "1"}


@pytest.mark.unit
def test_a_specs_environment_cannot_be_written_through() -> None:
    """Read-only means the mapping too, not just the attribute (ADR-0022)."""
    spec = a_run(_ANY_REPO).env({"A": "1"})

    with pytest.raises(TypeError):
        spec.environment["A"] = "changed"  # type: ignore[index]


@pytest.mark.unit
def test_a_spec_is_read_only() -> None:
    spec = a_run(_ANY_REPO)

    with pytest.raises(dataclasses.FrozenInstanceError):
        spec.base_ref = "main"  # type: ignore[misc]


@pytest.mark.unit
def test_the_builders_chain_in_any_order() -> None:
    spec = (
        a_run(_ANY_REPO)
        .base("main")
        .salvage(False)
        .timeouts(Timeouts(agent_wall=30.0))
        .integrate("agents/x", mechanism="merge")
    )

    assert spec.base_ref == "main"
    assert spec.salvaging is False
    assert spec.bounds.agent_wall == 30.0
    assert spec.integration == Integration("agents/x", mechanism="merge")


@pytest.mark.git
@pytest.mark.parametrize("name", [None, "fix-the-flake"])
async def test_a_runs_name_is_on_what_it_returns(
    host_repo: Path, name: str | None
) -> None:
    spec = a_run(host_repo) if name is None else a_run(host_repo).name(name)

    result = await spec

    assert isinstance(result, RunSucceeded), result
    assert result.name == name


@pytest.mark.git
async def test_a_failed_run_carries_its_name_too(host_repo: Path) -> None:
    result = await a_run(host_repo).agent(ShellAgent("exit 3")).name("doomed")

    assert isinstance(result, RunFailed), result
    assert result.name == "doomed"
