"""DockerSandbox: every run in a fresh container, from an image you built."""

from __future__ import annotations

import secrets
import shutil
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import get_args

from waystation.errors import PreflightError, StageError
from waystation.observability import SANDBOX
from waystation.results import CommandFailed, Errored, Refused
from waystation.sandbox._docker_plans import (
    RUN_ID_LABEL,
    WORKSPACE,
    Transport,
    plan_create,
    plan_destroy,
    plan_exec,
    plan_inspect,
    plan_ping,
    resolve_transport,
)
from waystation.sandbox._host import allowlisted_env, discard_workspace, run_exec
from waystation.sandbox.processes import ProcessStrategy, ProcessTree, host_processes
from waystation.sandbox.protocol import ExecResult, LineCallback, Sandbox
from waystation.sandbox.transport import clone_in
from waystation.tails import bound_tail
from waystation.workspace import Workspace

__all__ = ["DockerSandbox"]


@dataclass(slots=True)
class _Container:
    """A running sandbox: each exec is a ``docker exec`` into it."""

    name: str
    workspace: str = WORKSPACE
    _processes: ProcessStrategy = field(default_factory=host_processes)
    _trees: list[ProcessTree] = field(default_factory=list)

    async def exec(
        self,
        argv: Sequence[str],
        *,
        stdin: str | None = None,
        env: Mapping[str, str] | None = None,
        capture: bool = True,
        on_stdout: LineCallback | None = None,
        on_stderr: LineCallback | None = None,
    ) -> ExecResult:
        # The docker client inherits the host's environment, which it needs
        # to find its daemon; the container sees only the -e it is given.
        return await run_exec(
            plan_exec(self.name, argv, env=env or {}, stdin=stdin is not None),
            processes=self._processes,
            trees=self._trees,
            stdin=stdin,
            capture=capture,
            on_stdout=on_stdout,
            on_stderr=on_stderr,
        )

    def release_trees(self) -> None:
        for tree in self._trees:
            tree.release()
        self._trees.clear()


@dataclass(frozen=True, slots=True)
class DockerSandbox:
    """Each run in a fresh container from ``image``, removed with the run.

    The image is yours to build; waystation never builds or pulls one
    (ADR-0011), and preflight fails with a build hint when it is missing. The
    container runs as the image's own ``USER``, idling until work is exec'd
    into it, and is torn down with ``docker rm -f`` (ADR-0014). The image
    needs ``git`` and ``sh``.

    ``transport`` is how the workspace gets in (ADR-0012): ``"auto"`` copies
    on Windows and binds elsewhere. ``"bind"`` mounts the host workspace at
    ``/workspace``, so the image's user must be the host user's uid on Linux.
    ``"copy"`` clones it in with ``clone_in``, into a ``/workspace`` the image
    gives its user — ``RUN install -d -o <user> /workspace`` — and needs
    ``base64`` too (ADR-0028).

    ``env`` and ``pass_env`` are all of the environment it gets (ADR-0013);
    ``run_args`` go to ``docker run`` as they are, for networks, limits and
    mounts waystation has no setting for.
    """

    image: str
    env: Mapping[str, str] = field(default_factory=dict)
    pass_env: Sequence[str] = ()
    transport: Transport = "auto"
    run_args: Sequence[str] = ()

    def __post_init__(self) -> None:
        if self.transport not in get_args(Transport):
            allowed = get_args(Transport)
            msg = f"transport must be one of {allowed}, not {self.transport!r}"
            raise ValueError(msg)
        for name in ("pass_env", "run_args"):
            if isinstance(getattr(self, name), str):
                msg = f"{name} takes a sequence of strings, not one string"
                raise TypeError(msg)
        # A spec is a value: equal specs compare equal whatever sequence type
        # built them, and a caller's dict changing later changes no spec.
        object.__setattr__(self, "env", dict(self.env))
        object.__setattr__(self, "pass_env", tuple(self.pass_env))
        object.__setattr__(self, "run_args", tuple(self.run_args))

    async def preflight(self) -> None:
        """The daemon answers and the image is here; nothing is built or pulled.

        One docker call when all is well; a second only to say which is wrong.
        """
        if shutil.which("docker") is None:
            missing = FileNotFoundError("docker CLI not found on PATH")
            raise PreflightError(
                "docker CLI not found on PATH: install Docker to use DockerSandbox",
                failure=Errored(exception=missing),
            )
        if (await _docker(plan_inspect(self.image))).exit_code == 0:
            return
        ping = plan_ping()
        answered = await _docker(ping)
        if answered.exit_code != 0:
            raise PreflightError(
                "Docker daemon not reachable: start Docker, then retry",
                failure=CommandFailed(
                    argv=ping,
                    exit_code=answered.exit_code,
                    stderr_tail=bound_tail(answered.stderr),
                ),
            )
        detail = (
            f"image {self.image!r} is not on this host, and waystation never "
            f"builds or pulls one (ADR-0011): build it first, e.g. "
            f"`docker build -t {self.image} <context-dir>`"
        )
        raise PreflightError(
            detail, failure=Refused(reason="image_missing", detail=detail)
        )

    @asynccontextmanager
    async def start(
        self,
        ws: Workspace,
        *,
        env: Mapping[str, str],
        pass_env: Sequence[str],
    ) -> AsyncIterator[Sandbox]:
        transport = resolve_transport(self.transport)
        # Named before it exists, so teardown can remove it even when a bound
        # kills `docker run` before it says what it made.
        container = _Container(name=f"waystation-{ws.run_id}-{secrets.token_hex(3)}")
        create = plan_create(
            image=self.image,
            name=container.name,
            run_id=ws.run_id,
            env=allowlisted_env(
                literal={**self.env, **env},
                pass_env=(*self.pass_env, *pass_env),
            ),
            bind=str(ws.path) if transport == "bind" else None,
            run_args=self.run_args,
        )
        try:
            created = await _docker(create)
            if created.exit_code != 0:
                raise StageError(
                    "sandbox",
                    CommandFailed(
                        argv=create,
                        exit_code=created.exit_code,
                        stderr_tail=bound_tail(created.stderr),
                    ),
                )
            if transport == "copy":
                await clone_in(container, ws)
            yield container
        finally:
            container.release_trees()
            try:
                await _destroy(container.name)
            finally:
                discard_workspace(ws.path)


async def _docker(argv: Sequence[str]) -> ExecResult:
    """One docker CLI call on the host, killed with its tree if cancelled."""
    trees: list[ProcessTree] = []
    try:
        return await run_exec(argv, processes=host_processes(), trees=trees)
    finally:
        for tree in trees:
            tree.release()


async def _destroy(name: str) -> None:
    """Remove the container; a failure is logged, never raised (ADR-0016).

    Removing a container that never came to be succeeds, so a start killed
    mid-``docker run`` tears down the same way as any other.
    """
    removed = await _docker(plan_destroy(name))
    if removed.exit_code != 0:
        SANDBOX.error(
            "teardown failed to remove container %s (labelled %s): %s",
            name,
            RUN_ID_LABEL,
            bound_tail(removed.stderr),
        )
