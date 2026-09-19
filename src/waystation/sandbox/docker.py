"""DockerSandbox: every run in a fresh container, from an image you built."""

from __future__ import annotations

import asyncio
import secrets
import shutil
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import get_args

from waystation._cancellation import run_to_end
from waystation.errors import PreflightError, StageError
from waystation.observability import SANDBOX
from waystation.results import CommandFailed, Errored, Failure, Refused
from waystation.sandbox._docker_plans import (
    RUN_ID_LABEL,
    WORKSPACE,
    Transport,
    plan_create,
    plan_destroy,
    plan_exec,
    plan_inspect,
    plan_kill,
    plan_labelled,
    plan_ping,
    resolve_transport,
)
from waystation.sandbox._host import HostRunner, allowlisted_env, discard_workspace
from waystation.sandbox.processes import host_processes
from waystation.sandbox.protocol import ExecResult, LineCallback, Sandbox
from waystation.sandbox.transport import clone_in
from waystation.tails import bound_tail
from waystation.workspace import Workspace

__all__ = ["DockerSandbox"]


@dataclass(slots=True)
class _Container:
    """A running sandbox: each exec is a ``docker exec`` into it."""

    name: str
    _runner: HostRunner
    workspace: str = WORKSPACE

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
        group = secrets.token_hex(4)
        # The docker client inherits the host's environment, which it needs
        # to find its daemon; the container sees only the -e it is given.
        planned = plan_exec(
            self.name, argv, env=env or {}, stdin=stdin is not None, group=group
        )
        try:
            return await self._runner.run(
                planned,
                stdin=stdin,
                capture=capture,
                on_stdout=on_stdout,
                on_stderr=on_stderr,
            )
        except asyncio.CancelledError:
            # The runner killed the client, which leaves what it started in
            # the container running: that goes too, before the cancellation
            # does (ADR-0023). One more cancellation meanwhile adds nothing
            # to the one already on its way.
            await run_to_end(self._kill(group), lambda _: None)
            raise

    async def _kill(self, group: str) -> None:
        """Kill a cancelled exec's group; a failure is logged, never raised."""
        killed = await self._runner.run(plan_kill(self.name, group))
        if killed.exit_code != 0:
            SANDBOX.error(
                "failed to kill a cancelled exec in container %s: %s",
                self.name,
                bound_tail(killed.stderr),
            )


@dataclass(frozen=True, slots=True)
class DockerSandbox:
    """Each run in a fresh container from ``image``, removed with the run.

    The image is yours to build; waystation never builds or pulls one
    (ADR-0011), and preflight fails with a build hint when it is missing. The
    container runs as the image's own ``USER``, idling until work is exec'd
    into it, and is torn down with ``docker rm -f`` (ADR-0014). The image
    needs ``git``, ``sh``, and a ``sleep`` that takes ``infinity`` (coreutils
    and busybox both do): that is the idle process. It needs a writable
    ``/tmp`` too, where each exec records its process group, so that a
    cancelled exec is killed with everything it started (ADR-0023); with
    ``--read-only`` in ``run_args``, add ``--tmpfs /tmp``.

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
                failure=_failed(ping, answered),
            )
        refused = _image_missing(self.image)
        raise PreflightError(refused.detail, failure=refused)

    @staticmethod
    async def reap(run_id: str | None = None) -> int:
        """Remove the sandboxes waystation made, or only ``run_id``'s; say how many.

        Nothing calls it. A run removes its own sandbox, so one still here is
        an orphan of a run killed too hard to tear down — or the live sandbox
        of a flow running now, which only you can tell apart (ADR-0014). Name
        the run when you can.

        Raises ``StageError("sandbox", CommandFailed)`` when docker fails.
        """
        found = await _docker_or_raise(plan_labelled(run_id))
        ids = found.stdout.split()
        if ids:
            await _docker_or_raise(plan_destroy(*ids))
        return len(ids)

    @asynccontextmanager
    async def start(
        self,
        ws: Workspace,
        *,
        env: Mapping[str, str],
        pass_env: Sequence[str],
    ) -> AsyncIterator[Sandbox]:
        transport = resolve_transport(self.transport)
        # Named before it exists, so teardown can name it even when a bound
        # kills `docker run` before it says what it made. A create the daemon
        # finishes only after that `rm -f` is left running, found by its run
        # label (ADR-0014). The suffix keeps two starts of one run id apart.
        runner = HostRunner(host_processes())
        container = _Container(
            name=f"waystation-{ws.run_id}-{secrets.token_hex(3)}", _runner=runner
        )
        create = plan_create(
            image=self.image,
            name=container.name,
            run_id=ws.run_id,
            env=allowlisted_env(
                literal={**self.env, **env},
                pass_env=(*self.pass_env, *pass_env),
            ),
            bind_source=str(ws.path) if transport == "bind" else None,
            run_args=self.run_args,
        )
        try:
            created = await _docker(create)
            if created.exit_code != 0:
                raise StageError("sandbox", await self._why_not(create, created))
            if transport == "copy":
                await clone_in(container, ws)
            yield container
        finally:
            runner.release()
            try:
                await _destroy(container.name)
            finally:
                discard_workspace(ws.path)

    async def _why_not(self, create: Sequence[str], created: ExecResult) -> Failure:
        """Why ``docker run`` failed: an image gone since preflight is refused.

        Waystation asks for the image itself rather than read docker's stderr,
        which never becomes a refusal (ADR-0016); and only a daemon that
        answers can say an image is missing.
        """
        image_gone = (await _docker(plan_inspect(self.image))).exit_code != 0
        if image_gone and (await _docker(plan_ping())).exit_code == 0:
            return _image_missing(self.image)
        return _failed(create, created)


def _image_missing(image: str) -> Refused:
    detail = (
        f"image {image!r} is not on this host, and waystation never "
        f"builds or pulls one (ADR-0011): build it first, e.g. "
        f"`docker build -t {image} <context-dir>`"
    )
    return Refused(reason="image_missing", detail=detail)


async def _docker(argv: Sequence[str]) -> ExecResult:
    """One docker CLI call on the host, killed with its tree if cancelled."""
    runner = HostRunner(host_processes())
    try:
        return await runner.run(argv)
    finally:
        runner.release()


async def _docker_or_raise(argv: Sequence[str]) -> ExecResult:
    """One docker CLI call that must succeed, or the sandbox stage fails."""
    result = await _docker(argv)
    if result.exit_code != 0:
        raise StageError("sandbox", _failed(argv, result))
    return result


def _failed(argv: Sequence[str], result: ExecResult) -> CommandFailed:
    return CommandFailed(
        argv=argv, exit_code=result.exit_code, stderr_tail=bound_tail(result.stderr)
    )


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
