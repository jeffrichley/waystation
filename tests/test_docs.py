"""The indexes agents navigate by, kept honest (issue #33).

An index nobody maintains is worse than no index: it tells the next reader a
thing doesn't exist. These are cheap enough to run on every commit, so drift
fails here rather than being discovered by someone who trusted the table.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
ADR_DIR = REPO / "docs" / "adr"
TESTS_GUIDE = REPO / "tests" / "CLAUDE.md"


@pytest.mark.unit
def test_every_adr_is_listed_in_the_decision_index() -> None:
    """CLAUDE.md sends agents to the index; an ADR missing from it is invisible."""
    index = (ADR_DIR / "README.md").read_text(encoding="utf-8")
    adrs = {path.name for path in ADR_DIR.glob("*.md")} - {"README.md"}

    missing = sorted(name for name in adrs if name not in index)
    assert not missing, (
        f"not linked from docs/adr/README.md: {missing}. "
        "Add a row with a 'read it when you're…' line."
    )


@pytest.mark.unit
def test_every_shared_fixture_and_helper_is_listed_for_agents() -> None:
    """A helper nobody can find gets copy-pasted instead — thirteen times, once.

    The bar is that the guide names it as code somewhere, not which row it sits
    in: that catches the helper added and never written down, which is how the
    tables actually rot.
    """
    listed = _code_spans(TESTS_GUIDE)
    shared = _fixture_names(REPO / "tests" / "conftest.py") | _exported_names(
        REPO / "tests" / "helpers.py"
    )

    missing = sorted(shared - listed)
    assert not missing, (
        f"not listed in tests/CLAUDE.md: {missing}. "
        "Add a row so the next test reaches for it instead of writing its own."
    )


def _code_spans(path: Path) -> set[str]:
    """Every name a markdown file names in backticks, ``git(repo, …)`` as ``git``.

    Prose is not a listing: matching the whole file as a substring would let
    ``sh`` pass on the word "shell", so a name counts only where it is marked
    up as code.
    """
    spans = re.findall(r"`([^`]+)`", path.read_text(encoding="utf-8"))
    return {re.split(r"[(\s]", span, maxsplit=1)[0] for span in spans}


def _fixture_names(path: Path) -> set[str]:
    """Names of every ``@pytest.fixture`` defined at the top level of ``path``."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and any("fixture" in ast.dump(decorator) for decorator in node.decorator_list)
    }


def _exported_names(path: Path) -> set[str]:
    """The ``__all__`` of ``path``, read without importing it."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__all__"
            for target in node.targets
        ):
            return {
                name for name in ast.literal_eval(node.value) if isinstance(name, str)
            }
    return set()


@pytest.mark.unit
def test_every_module_curates_its_public_surface() -> None:
    """CLAUDE.md: every module curates ``__all__``, the rest underscore-private.

    A module without one exports whatever it happens to have imported, so
    ``from x import *`` and every reader's idea of the seam drift apart. The
    private modules follow the rule too: underscore means the *module* is
    private, not that its contents are unnamed.
    """
    modules = sorted(p for p in (REPO / "src" / "waystation").rglob("*.py"))
    missing = [
        p.relative_to(REPO).as_posix() for p in modules if not _exported_names(p)
    ]

    assert not missing, (
        f"no __all__: {missing}. Curate the names the module means to export; "
        "everything else is underscore-private (CLAUDE.md)."
    )
