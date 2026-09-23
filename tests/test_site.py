"""The documentation site stays built from the code, not copied from it (#127).

The API reference is generated from each public module's ``__all__``, since
that list is the contract (#18), and every tutorial page pulls its rung in by
snippet include, so a page can't drift from the script CI type-checks.
``mkdocs build --strict`` in ``just check`` catches a broken include or
reference; these catch a page that quietly stopped covering something.
"""

from __future__ import annotations

import importlib
import importlib.util
import re
from collections import Counter
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parent.parent
SITE = REPO / "site"
HOOK = SITE / "hooks" / "api_reference.py"

# The modules #18's public import surface names: the top level, the three seams
# and the test support a user implements against. Plus the ordering seam, which
# lives in its own module and nowhere else (ADR-0048), though #18 predates it.
PUBLIC_MODULES = {
    "waystation",
    "waystation.agents",
    "waystation.integration",
    "waystation.ordering",
    "waystation.sandbox",
    "waystation.testing",
}


def _hook() -> ModuleType:
    """The reference hook, loaded from its file: ``site/`` is no package."""
    spec = importlib.util.spec_from_file_location("api_reference", HOOK)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pages(under: str) -> dict[Path, str]:
    """Every site page under ``site/docs/<under>``, with its source."""
    return {
        path: path.read_text(encoding="utf-8")
        for path in (SITE / "docs" / under).rglob("*.md")
    }


@pytest.mark.unit
def test_a_reference_marker_documents_exactly_the_modules_all() -> None:
    """One ``:::`` block per name in ``__all__``, and nothing it doesn't list."""
    rendered = _hook().on_page_markdown("# Agents\n\n<!-- api: waystation.agents -->\n")

    documented = re.findall(
        r"^::: waystation\.agents\.(?:\w+\.)?(\w+)$", rendered, re.M
    )
    assert documented == list(importlib.import_module("waystation.agents").__all__)
    assert rendered.startswith("# Agents\n")
    assert "<!-- api:" not in rendered


@pytest.mark.unit
def test_a_function_named_after_its_module_is_documented_where_it_is_defined() -> None:
    """``waystation.queue`` is a module as well as the function it exports.

    Read by path, ``::: waystation.queue`` documents the module, and the
    function's page grows every other name the module holds. The function is
    documented by the path it is defined at, so the block is the function.
    """
    rendered = _hook().on_page_markdown("<!-- api: waystation -->")
    blocks = set(re.findall(r"^::: ([\w.]+)$", rendered, re.MULTILINE))

    assert {"waystation.queue.queue", "waystation.agents.run_agent.run_agent"} <= blocks
    assert not {"waystation.queue", "waystation.run_agent"} & blocks


@pytest.mark.unit
def test_a_page_without_a_marker_is_left_alone() -> None:
    """Every page passes through the hook; only a marker is rewritten."""
    page = "# Install\n\nNo reference here.\n"

    assert _hook().on_page_markdown(page) == page


@pytest.mark.unit
def test_the_reference_covers_every_public_module() -> None:
    """A seam module left out of the reference is a contract nobody can read."""
    marked = {
        module
        for text in _pages("reference").values()
        for module in re.findall(r"<!-- api: ([\w.]+) -->", text)
    }

    assert marked == PUBLIC_MODULES


@pytest.mark.unit
def test_every_page_is_in_the_nav_and_in_llms_txt() -> None:
    """Both lists are written out by hand, in reading order; each needs every page.

    mkdocs only logs a page missing from the nav, and ``llms.txt`` silently
    drops one missing from its sections, so neither build fails for it.
    """
    config = (SITE / "mkdocs.yml").read_text(encoding="utf-8")
    listed = Counter(re.findall(r"(?:- |: )([\w/-]+\.md)$", config, re.MULTILINE))
    pages = {path.relative_to(SITE / "docs").as_posix() for path in _pages("")}

    assert {page: listed[page] for page in pages} == dict.fromkeys(pages, 2)


@pytest.mark.unit
def test_every_example_is_included_in_the_tutorial_never_copied() -> None:
    """Each script is on a page by snippet include, and no page pastes code.

    A pasted copy is the drift the includes exist to prevent, so a tutorial
    page has no Python of its own: its code is the file CI type-checks.
    """
    pages = _pages("tutorial")
    included = {
        name
        for text in pages.values()
        for name in re.findall(r'--8<-- "examples/(\w+\.py)"', text)
    }
    examples = {path.name for path in (REPO / "examples").glob("*.py")}

    assert included == examples
    pasted = [
        path.name
        for path, text in pages.items()
        for body in re.findall(r"```python[^\n]*\n(.*?)```", text, re.DOTALL)
        if not re.fullmatch(r'--8<-- "[^"]+"\n', body)
    ]
    assert not pasted, f"tutorial pages with Python of their own: {pasted}"
