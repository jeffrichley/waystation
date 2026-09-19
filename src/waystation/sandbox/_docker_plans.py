"""Pure argv plans for DockerSandbox: what each docker call says, and nothing more.

Every plan is a function of its arguments alone, so labels, user, transport,
the environment allowlist and ``run_args`` placement unit-test without Docker
(ADR-0010); the one runner that executes them lives in ``_host``.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping, Sequence
from typing import Literal

__all__ = [
    "RUN_ID_LABEL",
    "WORKSPACE",
    "Transport",
    "plan_create",
    "plan_destroy",
    "plan_exec",
    "plan_inspect",
    "plan_ping",
    "resolve_transport",
]

Transport = Literal["auto", "copy", "bind"]

WORKSPACE = "/workspace"
"""The workspace root in every container, and every exec's working directory."""

RUN_ID_LABEL = "waystation.run-id"
"""Every sandbox carries it, so an orphan is findable by run (ADR-0014)."""


def resolve_transport(
    transport: Transport, *, platform: str = sys.platform
) -> Literal["copy", "bind"]:
    """``auto`` copies on Windows and binds elsewhere (ADR-0012)."""
    if transport != "auto":
        return transport
    return "copy" if platform == "win32" else "bind"


def _env_flags(env: Mapping[str, str]) -> list[str]:
    # Values ride the argv, as they would in a user's own docker call; every
    # logged command line and CommandFailed elides them by key (ADR-0025).
    flags: list[str] = []
    for key, value in env.items():
        flags += ["-e", f"{key}={value}"]
    return flags


def plan_create(
    *,
    image: str,
    name: str,
    run_id: str,
    env: Mapping[str, str],
    bind: str | None,
    run_args: Sequence[str],
) -> tuple[str, ...]:
    """``docker run -d`` of an idle process, as the image's own user (ADR-0014).

    No ``--user``: the image's ``USER`` is the sandbox's. ``--pull=never``
    makes a missing image fail rather than be fetched (ADR-0011). ``run_args``
    come after waystation's own options, so theirs win where both set one.
    """
    argv = [
        "docker",
        "run",
        "-d",
        "--pull=never",
        "--name",
        name,
        "--label",
        f"{RUN_ID_LABEL}={run_id}",
        "--workdir",
        WORKSPACE,
        *_env_flags(env),
    ]
    if bind is not None:
        argv += ["--mount", f"type=bind,source={bind},target={WORKSPACE}"]
    argv += ["--entrypoint", "sleep", *run_args, image, "infinity"]
    return tuple(argv)


def plan_exec(
    container: str,
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    stdin: bool,
) -> tuple[str, ...]:
    """``docker exec`` of ``argv`` in the workspace root, stdin open if given."""
    return (
        "docker",
        "exec",
        *(("-i",) if stdin else ()),
        "--workdir",
        WORKSPACE,
        *_env_flags(env),
        container,
        *argv,
    )


def plan_destroy(container: str) -> tuple[str, ...]:
    """``docker rm -f``, never ``stop`` (ADR-0014).

    ``-v`` takes any anonymous volume the image declares along with it.
    """
    return ("docker", "rm", "-f", "-v", container)


def plan_inspect(image: str) -> tuple[str, ...]:
    """Is the image here? Fails if it is not, or if the daemon is not."""
    return ("docker", "image", "inspect", "--format", "{{.Id}}", image)


def plan_ping() -> tuple[str, ...]:
    """Is the daemon reachable? Asked only once an inspect has failed."""
    return ("docker", "version", "--format", "{{.Server.Version}}")
