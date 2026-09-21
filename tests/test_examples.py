"""The example ladder's habits, held by a test rather than a README (issue #39).

CI never executes an example: a real agent costs money and needs Docker. What
it can do is read them. Every rung sets explicit ``Timeouts`` and calls
``configure_logging()`` and ``handle_signals()`` (#18), because a copied script
should start with good habits, and a rung that quietly drops one teaches the
opposite. These read the source without importing it.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
EXAMPLES = REPO / "examples"
INDEX = EXAMPLES / "README.md"


def _rungs() -> list[Path]:
    """The rungs the index lists under its ladder heading, in order."""
    text = INDEX.read_text(encoding="utf-8")
    ladder = text.split("## The ladder", 1)[1].split("\n## ", 1)[0]
    return [EXAMPLES / name for name in re.findall(r"\]\(([\w-]+\.py)\)", ladder)]


def _calls(tree: ast.Module) -> list[ast.Call]:
    return [node for node in ast.walk(tree) if isinstance(node, ast.Call)]


def _name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


@pytest.mark.unit
def test_the_ladder_starts_with_single_run() -> None:
    assert [rung.name for rung in _rungs()][:1] == ["single_run.py"]


@pytest.mark.unit
def test_every_example_is_listed_in_the_examples_index() -> None:
    """An example the index doesn't name is one a newcomer never finds."""
    index = INDEX.read_text(encoding="utf-8")
    missing = sorted(
        path.name for path in EXAMPLES.glob("*.py") if f"]({path.name})" not in index
    )
    assert not missing, f"not linked from examples/README.md: {missing}"


@pytest.mark.unit
def test_every_listed_rung_exists() -> None:
    missing = [rung.name for rung in _rungs() if not rung.is_file()]
    assert not missing, f"examples/README.md lists rungs that aren't there: {missing}"


@pytest.mark.unit
@pytest.mark.parametrize("rung", _rungs(), ids=lambda rung: rung.stem)
def test_every_rung_starts_with_good_habits(rung: Path) -> None:
    """Tutorial docstring, explicit ``Timeouts``, logging and signals (#18)."""
    tree = ast.parse(rung.read_text(encoding="utf-8"))
    calls = _calls(tree)
    names = {_name(call) for call in calls}

    assert ast.get_docstring(tree), f"{rung.name} has no tutorial docstring"
    assert "configure_logging" in names, f"{rung.name} never calls configure_logging"
    assert "handle_signals" in names, f"{rung.name} never calls handle_signals"
    timeouts = [call for call in calls if _name(call) == "Timeouts"]
    assert any(call.keywords for call in timeouts), (
        f"{rung.name} sets no explicit Timeouts: every bound defaults to "
        "unbounded (ADR-0017), and a copied script should choose its own"
    )


@pytest.mark.unit
def test_the_root_readme_sends_newcomers_to_the_examples() -> None:
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    assert "](examples/README.md)" in readme
