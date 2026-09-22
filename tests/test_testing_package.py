"""``waystation.testing`` is ScriptedAgent's home, and pytest is only the suite's.

#18's import surface and story 114 put ``ScriptedAgent`` here; the
conformance suite in the same package needs pytest (``waystation[testing]``),
so it is resolved lazily and a flow script's tests can play an agent back
without it (ADR-0035, #142).
"""

from __future__ import annotations

import subprocess
import sys

import pytest

import waystation
import waystation.agents
import waystation.testing


def _without_pytest(code: str) -> subprocess.CompletedProcess[str]:
    """Run ``code`` in a fresh interpreter where importing pytest fails."""
    blocked = "import sys\nsys.modules['pytest'] = None\n"
    return subprocess.run(
        [sys.executable, "-c", blocked + code],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.unit
def test_the_testing_package_exports_the_scripted_agent_beside_the_suite() -> None:
    assert set(waystation.testing.__all__) == {
        "SandboxConformance",
        "ScriptedAgent",
        "ScriptedCommit",
    }


@pytest.mark.unit
@pytest.mark.parametrize("package", [waystation, waystation.agents])
def test_the_scripted_agent_has_one_home(package: object) -> None:
    """A test double is not part of the production surface #18 lists."""
    exported = set(getattr(package, "__all__"))  # noqa: B009 - typed as object

    assert not exported & {"ScriptedAgent", "ScriptedCommit"}


@pytest.mark.unit
def test_the_scripted_agent_imports_without_pytest() -> None:
    result = _without_pytest(
        "from waystation.testing import ScriptedAgent, ScriptedCommit\n"
        "ScriptedAgent(commits=[ScriptedCommit('c', {'a.txt': 'a'})])\n"
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.unit
def test_only_touching_the_suite_needs_pytest() -> None:
    result = _without_pytest(
        "import waystation.testing\n"
        "try:\n"
        "    waystation.testing.SandboxConformance\n"
        "except ModuleNotFoundError as error:\n"
        "    print(error.name)\n"
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "pytest"


@pytest.mark.unit
def test_an_unknown_name_is_still_an_attribute_error() -> None:
    with pytest.raises(AttributeError, match="Nonesuch"):
        getattr(waystation.testing, "Nonesuch")  # noqa: B009 - the lookup is the test
