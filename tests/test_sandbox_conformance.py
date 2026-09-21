"""Every backend runs the one suite that states the exec contract (ADR-0035).

The suite itself ships, in ``waystation.testing``, because a backend
waystation does not ship has the same contract to keep. What is here is the
three backends pointed at it, and the guard that keeps the third one honest.
"""

from __future__ import annotations

import ast
from collections.abc import Sequence
from pathlib import Path

import pytest

from helpers import sh
from thin_backend import ThinHost
from waystation import DockerSandbox, NoSandbox, SandboxBackend
from waystation.testing import SandboxConformance


@pytest.mark.git
class TestNoSandboxConforms(SandboxConformance):
    @pytest.fixture
    def backend(self) -> SandboxBackend:
        return NoSandbox()

    @pytest.fixture
    def shell(self) -> Sequence[str]:
        # This backend's execs are host processes, and Windows has no `sh`
        # on PATH; naming one is the sandbox's job once #77 lands.
        return (sh(), "-c")


@pytest.mark.docker
class TestDockerSandboxConforms(SandboxConformance):
    @pytest.fixture
    def backend(self, image: str) -> SandboxBackend:
        return DockerSandbox(image)


@pytest.mark.git
class TestABackendOffThePublicSurfaceConforms(SandboxConformance):
    """The proof that no private import is needed: ``ThinHost`` has none."""

    @pytest.fixture
    def backend(self) -> SandboxBackend:
        return ThinHost()

    @pytest.fixture
    def shell(self) -> Sequence[str]:
        return (sh(), "-c")


@pytest.mark.unit
def test_the_third_backend_imports_nothing_private() -> None:
    """A backend a user could write reaches only for names waystation exports.

    The suite passing proves the contract can be kept; this proves it was
    kept from outside. Without it, one private import would quietly make the
    other proof vacuous.
    """
    source = Path(__file__).parent / "thin_backend.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))

    private = sorted(
        name
        for module in _imported_modules(tree)
        if module.split(".")[0] == "waystation"
        for name in [module]
        if any(part.startswith("_") for part in module.split("."))
    )

    assert not private, (
        f"{source.name} imports private waystation modules: {private}. "
        "A third backend cannot, so neither may the one standing in for it."
    )


def _imported_modules(tree: ast.Module) -> list[str]:
    """Every module name the tree imports, ``from a.b import c`` as ``a.b``."""
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.append(node.module)
    return modules
