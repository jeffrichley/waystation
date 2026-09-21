"""A failed image lookup is not the same claim as a missing image (#97).

``docker image inspect`` fails the same way for an image that is absent and
for a daemon that will not answer, and preflight used to read every failure
as absence — so a present image came back as "build it first", which sends
someone to rebuild what they already have.

These drive the docker CLI through a stub rather than a daemon, because the
interesting cases are ones a working Docker will not produce on demand.
That reaches for a private seam, ``docker._docker``, on purpose: the
classification *is* internal, and it is the whole of what #97 changed.
``test_docker_plans`` does the same for the argv plans, and like those these
are the only cover this logic gets on a host with no Docker.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from waystation import DockerSandbox, PreflightError
from waystation.results import CommandFailed, Refused
from waystation.sandbox import docker as docker_module
from waystation.sandbox.protocol import ExecResult

IMAGE = "waystation-test"

_NO_SUCH_IMAGE = (
    'Error response from daemon: {"message":"No such image: ' + IMAGE + '"}'
)
_DAEMON_DOWN = "error during connect: dial tcp 127.0.0.1:1: connection refused"


def _speaks(**answers: ExecResult) -> object:
    """A stand-in ``_docker`` answering per subcommand; records what it was asked.

    Keys are ``inspect``, ``ls`` and ``version``; anything unasked-for fails
    the test rather than quietly returning a default.
    """
    asked: list[tuple[str, ...]] = []

    async def fake(argv: Sequence[str]) -> ExecResult:
        asked.append(tuple(argv))
        for name, answer in answers.items():
            if name in argv:
                return answer
        pytest.fail(f"nothing scripted for {list(argv)}")

    fake.asked = asked  # type: ignore[attr-defined]
    return fake


def _ok(stdout: str = "") -> ExecResult:
    return ExecResult(exit_code=0, stdout=stdout, stderr="")


def _fails(stderr: str) -> ExecResult:
    return ExecResult(exit_code=1, stdout="", stderr=stderr)


@pytest.mark.unit
async def test_an_image_that_is_here_passes_preflight_in_one_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The common path stays one docker call: nothing else is asked."""
    fake = _speaks(inspect=_ok("sha256:abc"))
    monkeypatch.setattr(docker_module, "_docker", fake)

    await DockerSandbox(IMAGE).preflight()

    assert len(fake.asked) == 1  # type: ignore[attr-defined]


@pytest.mark.unit
async def test_an_absent_image_is_refused_and_says_to_build_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The claim preflight may still make: docker answered, and said nothing."""
    monkeypatch.setattr(
        docker_module, "_docker", _speaks(inspect=_fails(_NO_SUCH_IMAGE), ls=_ok(""))
    )

    with pytest.raises(PreflightError) as raised:
        await DockerSandbox(IMAGE).preflight()

    assert raised.value.failure == Refused(
        reason="image_missing", detail=str(raised.value)
    )
    assert "build it first" in str(raised.value)


@pytest.mark.unit
async def test_an_image_that_is_here_but_will_not_inspect_is_not_called_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#97: docker says the image is here, so nobody is sent to rebuild it.

    Docker Desktop does exactly this after its VM auto-pauses: lookup by name
    fails while the image is listed and readable by id.
    """
    monkeypatch.setattr(
        docker_module,
        "_docker",
        _speaks(inspect=_fails(_NO_SUCH_IMAGE), ls=_ok("f07929985ab3")),
    )

    with pytest.raises(PreflightError) as raised:
        await DockerSandbox(IMAGE).preflight()

    failure = raised.value.failure
    assert not isinstance(failure, Refused), "a lookup that failed is not a refusal"
    assert "build it first" not in str(raised.value)
    assert "is on this host" in str(raised.value)
    # Docker's own words survive, which is what says where to look next.
    assert isinstance(failure, CommandFailed)
    assert _NO_SUCH_IMAGE in failure.stderr_tail


@pytest.mark.unit
async def test_a_daemon_that_will_not_answer_says_so_rather_than_blaming_the_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        docker_module,
        "_docker",
        _speaks(
            inspect=_fails(_DAEMON_DOWN),
            ls=_fails(_DAEMON_DOWN),
            version=_fails(_DAEMON_DOWN),
        ),
    )

    with pytest.raises(PreflightError, match="not reachable") as raised:
        await DockerSandbox(IMAGE).preflight()

    assert isinstance(raised.value.failure, CommandFailed)


@pytest.mark.unit
async def test_a_lookup_nothing_can_settle_says_that_much(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The daemon answers a ping but not a lookup: neither claim is available."""
    monkeypatch.setattr(
        docker_module,
        "_docker",
        _speaks(
            inspect=_fails("inspect broke"),
            ls=_fails("ls broke"),
            version=_ok("28.0.4"),
        ),
    )

    with pytest.raises(PreflightError, match="could not tell whether") as raised:
        await DockerSandbox(IMAGE).preflight()

    assert not isinstance(raised.value.failure, Refused)
    assert isinstance(raised.value.failure, CommandFailed)
    assert "ls broke" in raised.value.failure.stderr_tail
