"""The indexes agents navigate by, kept honest (issue #33).

An index nobody maintains is worse than no index: it tells the next reader a
thing doesn't exist. These are cheap enough to run on every commit, so drift
fails here rather than being discovered by someone who trusted the table.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import re
import tomllib
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parent.parent
ADR_DIR = REPO / "docs" / "adr"
TESTS_GUIDE = REPO / "tests" / "CLAUDE.md"


@pytest.mark.unit
def test_every_adr_is_listed_in_the_decision_index() -> None:
    """CLAUDE.md sends agents to the index; an ADR missing from it is invisible."""
    index = (ADR_DIR / "README.md").read_text(encoding="utf-8")
    adrs = {path.name for path in ADR_DIR.glob("[0-9][0-9][0-9][0-9]-*.md")}

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


def _defined_public_names(path: Path) -> set[str]:
    """The public names ``path`` defines at top level; imports are not definitions."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(
                t.id
                for target in node.targets
                for t in ast.walk(target)
                if isinstance(t, ast.Name)
            )
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.TypeAlias) and isinstance(node.name, ast.Name):
            names.add(node.name.id)
    return {name for name in names if not name.startswith("_")}


@pytest.mark.unit
def test_every_public_name_a_module_defines_is_in_its_all() -> None:
    """CLAUDE.md's second half: a name outside ``__all__`` is underscore-private.

    A public name left out of ``__all__`` is a seam nobody decided on (#94):
    either something else imports it, and the module means to offer it, or
    nothing does, and the underscore says so.
    """
    stray = [
        f"{p.relative_to(REPO).as_posix()}: {name}"
        for p in sorted((REPO / "src" / "waystation").rglob("*.py"))
        for name in sorted(_defined_public_names(p) - _exported_names(p))
    ]

    assert not stray, (
        f"public but not in __all__: {stray}. Export it or underscore it (CLAUDE.md)."
    )


@pytest.mark.unit
def test_the_coverage_floor_is_one_number_everywhere_it_is_written() -> None:
    """The floor lives in two config tables and two guides; they drift apart silently.

    It went from #18's 90% to 85% in one table with no reason recorded (#145),
    so every place that states it is held to the same number here.
    """
    config = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    addopts = config["tool"]["pytest"]["ini_options"]["addopts"]
    (pytest_floor,) = (
        int(opt.removeprefix("--cov-fail-under="))
        for opt in addopts
        if opt.startswith("--cov-fail-under=")
    )
    floors = {
        "pytest addopts": pytest_floor,
        "coverage.report": config["tool"]["coverage"]["report"]["fail_under"],
    }
    for guide in (REPO / "CLAUDE.md", TESTS_GUIDE):
        stated = re.findall(
            r"(\d+)% branch-coverage floor|floor is (\d+)% branch coverage",
            guide.read_text(encoding="utf-8"),
        )
        assert stated, f"{guide.relative_to(REPO)} no longer states the floor"
        for match in stated:
            floors[str(guide.relative_to(REPO))] = int("".join(match))

    assert len(set(floors.values())) == 1, f"coverage floors disagree: {floors}"


@pytest.mark.unit
def test_every_public_callable_documents_its_args_and_what_it_returns() -> None:
    """#147: the public API's docstrings are Google-style, sections and all.

    ruff's ``D`` rules hold that a docstring exists and is well-formed, and
    D417 that an ``Args:`` section names every parameter — but only once there
    is one. This holds the rest: a public callable that takes arguments says
    what they are, and one that gives something back says what. ``Raises:``
    stays with review; which exceptions escape is not something a signature
    tells.
    """
    missing = [
        f"{where}: {section}"
        for where, fn in _public_callables()
        for section in _sections_owed(fn)
        if f"{section}:" not in (inspect.getdoc(fn) or "")
    ]

    assert not missing, (
        f"missing Google-style sections: {missing}. "
        "Document them in the docstring (#147)."
    )


# The method wrappers whose function sits one attribute further in.
_BOUND_DESCRIPTORS = (classmethod, staticmethod)


def _public_callables() -> list[tuple[str, Callable[..., object]]]:
    """Every function and method a public module's ``__all__`` hands a user.

    A method counts where its class defines it: properties read as attributes,
    dunders mean what their protocol says (``__init__`` excepted, which takes
    the arguments), and the conformance suite's ``test_`` methods are tests,
    whose parameters are fixtures.
    """
    found: dict[int, tuple[str, Callable[..., object]]] = {}
    for module in _public_modules():
        for name in module.__all__:
            obj = getattr(module, name)
            if not getattr(obj, "__module__", "").startswith("waystation"):
                continue
            if inspect.isfunction(obj):
                found[id(obj)] = (f"{obj.__module__}.{name}", obj)
            elif inspect.isclass(obj):
                for attr, member in vars(obj).items():
                    fn = (
                        member.__func__
                        if isinstance(member, _BOUND_DESCRIPTORS)
                        else member
                    )
                    if (
                        inspect.isfunction(fn)
                        and fn.__module__.startswith("waystation")
                        and (attr == "__init__" or not attr.startswith("_"))
                        and not attr.startswith("test_")
                        # A dataclass writes its own __init__; its fields are
                        # the class's to document, not a docstring nobody wrote.
                        and fn.__code__.co_filename != "<string>"
                    ):
                        found[id(fn)] = (f"{obj.__module__}.{fn.__qualname__}", fn)
    return sorted(found.values(), key=lambda pair: pair[0])


def _public_modules() -> list[ModuleType]:
    """Every module under ``src/waystation`` with no underscore-private part."""
    root = REPO / "src"
    modules = []
    for path in sorted((root / "waystation").rglob("*.py")):
        parts = path.relative_to(root).with_suffix("").parts
        if parts[-1] == "__init__":
            parts = parts[:-1]
        if not any(part.startswith("_") for part in parts):
            modules.append(importlib.import_module(".".join(parts)))
    return modules


def _sections_owed(fn: Callable[..., object]) -> list[str]:
    """The sections ``fn``'s signature says its docstring owes a reader."""
    params = list(inspect.signature(fn).parameters)
    if params and params[0] in ("self", "cls"):
        params = params[1:]
    owed = ["Args"] if params else []
    returns = str(fn.__annotations__.get("return"))
    body = inspect.unwrap(fn)
    if inspect.isgeneratorfunction(body) or inspect.isasyncgenfunction(body):
        # A context manager that yields nothing has nothing to say it yields.
        if not returns.endswith("[None]"):
            owed.append("Yields")
    elif returns not in ("None", "NoReturn", "Never"):
        owed.append("Returns")
    return owed
