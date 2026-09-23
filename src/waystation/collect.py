"""Collect: salvage, linearity check, format-patch → PatchSeries."""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path

from waystation._git import run_git
from waystation.errors import StageError, attributing
from waystation.results import CommandFailed, Refused
from waystation.sandbox.protocol import Sandbox
from waystation.tails import bound_tail
from waystation.workspace import Workspace

__all__ = ["PatchSeries", "collect"]


@dataclass(frozen=True, slots=True)
class PatchSeries:
    """Ordered patches cut from ``base..HEAD`` (or built from a host range).

    ``salvaged`` says the last patch is work collect committed for the agent,
    which had left it uncommitted; a series built from a host range never is.
    """

    base_sha: str
    patches: tuple[str, ...]
    salvaged: bool = False

    @property
    def commits(self) -> int:
        """How many patches, so how many commits, the series holds."""
        return len(self.patches)

    @classmethod
    def from_format_patch(
        cls, base_sha: str, stdout: str, *, salvaged: bool = False
    ) -> PatchSeries:
        """The series ``git format-patch --stdout`` wrote, one patch per commit.

        Args:
            base_sha: The commit the series applies to.
            stdout: format-patch's whole output, decoded without newline
                translation.
            salvaged: Whether the last patch is work collect committed for
                the agent.

        Returns:
            The series, empty when ``stdout`` holds no patch.
        """
        return cls(
            base_sha=base_sha,
            patches=_split_format_patch(stdout),
            salvaged=salvaged,
        )

    @classmethod
    async def from_range(cls, repo: Path | str, base: str, ref: str) -> PatchSeries:
        """The series ``base..ref`` already holds on the host.

        Host-side and stage-less: this serves a landing and a resolver run
        alike, so a failure leaves it unattributed and whoever runs it as a
        stage names one (ADR-0032).

        Args:
            repo: The host repo holding both revisions.
            base: The revision the series applies to; resolved to a sha.
            ref: The revision the series ends at.

        Returns:
            One patch per commit in ``base..ref``, oldest first.

        Raises:
            StageError: ``Refused("nonlinear_series")`` when the range holds a
                merge commit, or ``ref`` does not descend from ``base``: a
                series is linear, and format-patch would flatten either one
                without a word (ADR-0006). Or a git failure. No stage is named
                yet.
        """
        host = Path(repo)
        verified = await run_git(host, "rev-parse", "--verify", base)
        base_sha = verified.stdout.strip()
        # rev-list first: a ref that names nothing fails as git's own error,
        # and is-ancestor is then left only its "no" to say.
        merges = await run_git(host, "rev-list", "--merges", f"{base_sha}..{ref}")
        descends = await run_git(
            host, "merge-base", "--is-ancestor", base_sha, ref, check=False
        )
        if descends.returncode != 0 or merges.stdout.strip():
            raise StageError(
                None,
                Refused(
                    reason="nonlinear_series",
                    detail=f"{base}..{ref}: {_NONLINEAR_DETAIL}",
                ),
            )
        result = await run_git(host, "format-patch", "--stdout", f"{base_sha}..{ref}")
        return cls.from_format_patch(base_sha, result.stdout)


def _split_format_patch(stdout: str) -> tuple[str, ...]:
    if not stdout.strip():
        return ()
    patches: list[str] = []
    current: list[str] = []
    for line in stdout.splitlines(keepends=True):
        if line.startswith("From ") and current:
            patches.append("".join(current))
            current = [line]
        else:
            current.append(line)
    if current and "".join(current).strip():
        patches.append("".join(current))
    return tuple(patches)


async def _sandbox_git(
    sandbox: Sandbox,
    *args: str,
    env: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    argv = ["git", *args]
    result = await sandbox.exec(argv, env=env, capture=True)
    if result.exit_code != 0:
        raise StageError(
            None,
            CommandFailed(
                argv=tuple(argv),
                exit_code=result.exit_code,
                stderr_tail=bound_tail(result.stderr),
            ),
        )
    return result.exit_code, result.stdout, result.stderr


_NONLINEAR_DETAIL = "series contains merge commits or HEAD does not descend from base"


async def collect(
    sandbox: Sandbox,
    workspace: Workspace,
    *,
    salvage: bool = True,
) -> PatchSeries:
    """Salvage, check linearity, and emit a ``PatchSeries`` via format-patch.

    A series that is not linear — the agent merged, or moved HEAD below base —
    is refused, not handed back: this raises ``StageError("collect", ...)``
    with ``Refused("nonlinear_series")`` (ADR-0006, ADR-0016). This is the
    collect stage, so this is the edge that names it (ADR-0032). The squash collect
    made of it rides on the error's ``series``, so a caller can still keep the
    work it will not land.

    Args:
        sandbox: The started sandbox that holds the workspace.
        workspace: The run's workspace; its ``base_sha`` starts the series.
        salvage: Commit whatever the agent left uncommitted, so it is the
            series' last patch, instead of leaving it behind.

    Returns:
        The linear series ``base..HEAD``.

    Raises:
        StageError: At ``"collect"``: ``Refused("nonlinear_series")`` with the
            squash on ``series`` when the series is not linear, or the failure
            of a git command the collect ran.
    """
    base = workspace.base_sha
    salvaged = False
    with attributing("collect"):
        verdict, patches = await _survey(sandbox, base, status=salvage)
        if verdict == "dirty":
            await _salvage(sandbox, workspace)
            salvaged = True
            verdict, patches = await _survey(sandbox, base, status=False)

        if verdict == "nonlinear":
            squashed = await _squash_series(sandbox, workspace, salvaged=salvaged)
            raise StageError(
                None,
                Refused(reason="nonlinear_series", detail=_NONLINEAR_DETAIL),
                series=squashed,
            )
    return PatchSeries.from_format_patch(base, patches, salvaged=salvaged)


# Collect's look at the workspace runs as this git alias. Git runs a `!` alias
# with its own sh, which a Windows host has even where no `sh` is on PATH, so
# a sandbox needs git and nothing else (ADR-0029).
_SURVEY = "waystation-collect"


async def _survey(sandbox: Sandbox, base: str, *, status: bool) -> tuple[str, str]:
    """One exec that looks the workspace over: its verdict, and the series if linear.

    The verdict is ``dirty`` (only when ``status``), ``nonlinear`` or
    ``linear``; the series is ``format-patch``'s output, as it would be from
    an exec of its own. Every exec is a ``docker exec`` on DockerSandbox,
    about 0.5 s on Docker Desktop, so the checks and the series are one.

    A git command that fails fails collect as it would alone: a
    ``CommandFailed`` with its argv, its exit code and only its stderr.
    """
    script, steps = _survey_script(base, status=status)
    argv = ("git", "-c", f"alias.{_SURVEY}=!{script}", _SURVEY)
    result = await sandbox.exec(argv, capture=True)
    if result.exit_code != 0:
        failed, stderr = _failed_step(steps, result.stderr) or (argv, result.stderr)
        raise StageError(
            None,
            CommandFailed(
                argv=failed, exit_code=result.exit_code, stderr_tail=bound_tail(stderr)
            ),
        )
    verdict, _, patches = result.stdout.partition("\n")
    return verdict, patches


def _survey_script(
    base: str, *, status: bool
) -> tuple[str, tuple[tuple[str, ...], ...]]:
    """The survey's script, and the argv of each step it runs, in order.

    Each step says on stderr that it starts, so the stderr of the one that
    failed is the stderr after its mark. Nothing reaches stdout before the
    verdict, and nothing but ``format-patch`` after it.
    """
    commits = f"{base}..HEAD"
    dirty = ("git", "status", "--porcelain")
    merges = ("git", "rev-list", "--merges", commits)
    descends = ("git", "merge-base", "--is-ancestor", base, "HEAD")
    series = ("git", "format-patch", "--stdout", commits)
    lines: list[str] = []
    if status:
        lines += [
            _mark(dirty),
            f"out=$({shlex.join(dirty)}) || exit",
            '[ -z "$out" ] || { echo dirty; exit 0; }',
        ]
    lines += [
        _mark(merges),
        f"out=$({shlex.join(merges)}) || exit",
        # Any exit of merge-base but 0 is nonlinear: not an ancestor, or no
        # telling whether it is.
        _mark(descends),
        f'if [ -n "$out" ] || ! {shlex.join(descends)}; then',
        "  echo nonlinear; exit 0",
        "fi",
        "echo linear",
        _mark(series),
        f"exec {shlex.join(series)}",
    ]
    steps = ((dirty,) if status else ()) + (merges, descends, series)
    return "\n".join(lines), steps


def _marker(argv: tuple[str, ...]) -> str:
    """The line a step writes to stderr as it starts."""
    return f"waystation: {shlex.join(argv)}"


def _mark(argv: tuple[str, ...]) -> str:
    return f"printf '%s\\n' {shlex.quote(_marker(argv))} >&2"


def _failed_step(
    steps: tuple[tuple[str, ...], ...], stderr: str
) -> tuple[tuple[str, ...], str] | None:
    """The last step that started, and the stderr it wrote; ``None`` if none did."""
    for argv in reversed(steps):
        marker = _marker(argv) + "\n"
        at = stderr.rfind(marker)
        if at >= 0:
            return argv, stderr[at + len(marker) :]
    return None


async def _salvage(sandbox: Sandbox, workspace: Workspace) -> None:
    """Commit everything uncommitted; fall back to a private index if needed.

    A git process killed mid-write leaves ``index.lock`` behind, which blocks
    ``git add`` on the agent's index. A private index seeded from ``HEAD``
    yields the same commit without touching that lock.
    """
    try:
        await _commit_worktree(sandbox, workspace)
    except StageError:
        _, git_dir, _ = await _sandbox_git(sandbox, "rev-parse", "--absolute-git-dir")
        env = {"GIT_INDEX_FILE": f"{git_dir.strip()}/waystation-salvage-index"}
        await _sandbox_git(sandbox, "read-tree", "HEAD", env=env)
        await _commit_worktree(sandbox, workspace, env=env)


async def _commit_worktree(
    sandbox: Sandbox, workspace: Workspace, env: dict[str, str] | None = None
) -> None:
    await _sandbox_git(sandbox, "add", "-A", env=env)
    await _sandbox_git(
        sandbox,
        "commit",
        "-m",
        "WIP: salvaged uncommitted work",
        "--trailer",
        f"Waystation-Run: {workspace.run_id}",
        env=env,
    )


async def _squash_series(
    sandbox: Sandbox, workspace: Workspace, *, salvaged: bool
) -> PatchSeries:
    """Replace a nonlinear range with one commit from ``diff --binary``."""
    base = workspace.base_sha
    _, diff, _ = await _sandbox_git(sandbox, "diff", "--binary", base, "HEAD")
    await _sandbox_git(sandbox, "reset", "--hard", base)
    if diff.strip():
        applied = await sandbox.exec(
            ["git", "apply", "--whitespace=nowarn"],
            stdin=diff,
            capture=True,
        )
        if applied.exit_code != 0:
            raise StageError(
                None,
                CommandFailed(
                    argv=("git", "apply"),
                    exit_code=applied.exit_code,
                    stderr_tail=bound_tail(applied.stderr),
                ),
            )
        await _sandbox_git(sandbox, "add", "-A")
        await _sandbox_git(
            sandbox,
            "commit",
            "-m",
            "WIP: squashed nonlinear series",
            "-m",
            f"Waystation-Run: {workspace.run_id}",
        )
    _, stdout, _ = await _sandbox_git(
        sandbox, "format-patch", "--stdout", f"{base}..HEAD"
    )
    return PatchSeries.from_format_patch(base, stdout, salvaged=salvaged)
