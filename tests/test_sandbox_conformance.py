"""Every backend runs the one suite that states the exec contract (ADR-0035).

The suite itself ships, in ``waystation.testing``, because a backend
waystation does not ship has the same contract to keep. What is here is the
three backends pointed at it, and the guard that keeps the third one honest.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from helpers import TRANSPORTS, Chosen
from thin_backend import ThinHost
from waystation import DockerSandbox, NoSandbox, SandboxBackend
from waystation.testing import SandboxConformance


@pytest.mark.git
class TestNoSandboxConforms(SandboxConformance):
    @pytest.fixture
    def backend(self) -> SandboxBackend:
        return NoSandbox()


@pytest.mark.docker
@pytest.mark.parametrize("transport", TRANSPORTS)
class TestDockerSandboxConforms(SandboxConformance):
    """The whole suite, once per transport (#106).

    Every promise under both, not just the transport-sensitive ones
    tests/CLAUDE.md names: a leg is 15 container starts, about 8 s serial on
    Docker Desktop, which is cheaper than deciding test by test.
    """

    @pytest.fixture
    def backend(self, image: str, transport: Chosen) -> SandboxBackend:
        return DockerSandbox(image, transport=transport)


@pytest.mark.git
class TestABackendOffThePublicSurfaceConforms(SandboxConformance):
    """The proof that no private import is needed: ``ThinHost`` has none."""

    @pytest.fixture
    def backend(self) -> SandboxBackend:
        return ThinHost()


@pytest.mark.unit
def test_the_third_backend_imports_nothing_private() -> None:
    """A backend a user could write reaches only for names waystation exports.

    The suite passing proves the contract can be kept; this proves it was
    kept from outside. Without it, one private import would quietly make the
    other proof vacuous.
    """
    source = Path(__file__).parent / "thin_backend.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))

    private = sorted(set(_private_waystation_imports(tree)))

    assert not private, (
        f"{source.name} reaches for private waystation names: {private}. "
        "A third backend cannot, so neither may the one standing in for it."
    )


def _private_waystation_imports(tree: ast.Module) -> list[str]:
    """Underscore-private waystation imports: a private module, or a private name.

    Both, because either would break the proof. A public module can hold a
    name its ``__all__`` leaves out, and importing that is as much a reach
    inside as importing ``waystation.sandbox._host`` was.
    """
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [a.name for a in node.names if _is_private(a.name)]
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            if not node.module.startswith("waystation"):
                continue
            if _is_private(node.module):
                found.append(node.module)
            found += [
                f"{node.module}.{a.name}" for a in node.names if a.name.startswith("_")
            ]
    return found


def _is_private(module: str) -> bool:
    parts = module.split(".")
    return parts[0] == "waystation" and any(part.startswith("_") for part in parts)
