"""The host's shell: found when asked, nameable, and never demanded (#101).

A flow script never names a shell — that is what keeps it running on
somebody else's machine. So the one override is an environment variable, the
shape Bazel's ``BAZEL_SH`` takes for this same problem (ADR-0036).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from helpers import init_host_repo
from waystation import NoSandbox, prepare_workspace
from waystation.sandbox import host_shell


@pytest.mark.unit
def test_the_host_names_its_own_shell_when_it_has_one() -> None:
    found = host_shell()

    assert Path(found[0]).is_file(), found  # absolute: Windows ignores a sandbox's PATH
    assert found[1] == "-c"


@pytest.mark.unit
def test_an_environment_variable_names_a_shell_instead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On the machine, never in the flow script, so the script stays portable."""
    monkeypatch.setenv("WAYSTATION_SH", "/somewhere/else/sh")

    assert host_shell() == ("/somewhere/else/sh", "-c")


@pytest.mark.unit
def test_a_host_with_no_shell_says_what_to_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("WAYSTATION_SH", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))  # no sh, and no git to find one by

    with pytest.raises(FileNotFoundError, match="WAYSTATION_SH"):
        host_shell()


@pytest.mark.git
async def test_a_sandbox_starts_on_a_host_whose_shell_cannot_be_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing resolves a shell until something wants one.

    Every provider that binds a CLI names an argv and never wants a shell,
    so a host without one runs them perfectly well. Preflight used to refuse
    such a host outright, which is the checking ADR-0036 turned down for an
    image, applied to the host instead.
    """
    ws = await prepare_workspace(init_host_repo(tmp_path))
    monkeypatch.delenv("WAYSTATION_SH", raising=False)

    await NoSandbox().preflight()  # no longer refuses a host without one

    async with NoSandbox().start(ws, env={}) as sandbox:
        assert sandbox.workspace  # started, with no shell resolved
        monkeypatch.setenv("PATH", str(tmp_path / "nothing-here"))
        with pytest.raises(FileNotFoundError, match="WAYSTATION_SH"):
            _ = sandbox.shell

    await ws.remove()


@pytest.mark.git
async def test_an_agent_that_names_an_argv_never_asks_for_a_shell(
    host_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run proves it: resolving one here would raise, and the run succeeds."""
    import waystation.sandbox.no_sandbox as backend

    def _no_shell_here() -> tuple[str, ...]:
        raise AssertionError("a run naming an argv asked for a shell")

    monkeypatch.setattr(backend, "host_shell", _no_shell_here)
    ws = await prepare_workspace(host_repo)

    async with NoSandbox().start(ws, env={}) as sandbox:
        shown = await sandbox.exec(["git", "rev-parse", "--show-prefix"])

    await ws.remove()
    assert shown.exit_code == 0
