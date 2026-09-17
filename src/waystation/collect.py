"""Collect: salvage, linearity check, format-patch → PatchSeries."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from waystation._git import run_git
from waystation.errors import StageError
from waystation.results import CommandFailed, Series
from waystation.sandbox.protocol import Sandbox
from waystation.tails import bound_tail
from waystation.workspace import Workspace


@dataclass(frozen=True, slots=True)
class PatchSeries:
    """Ordered patches cut from ``base..HEAD`` (or built from a host range)."""

    base_sha: str
    patches: tuple[str, ...]

    @property
    def commits(self) -> int:
        return len(self.patches)

    @classmethod
    def from_format_patch(cls, base_sha: str, stdout: str) -> PatchSeries:
        return cls(base_sha=base_sha, patches=_split_format_patch(stdout))

    @classmethod
    def from_range(cls, repo: Path | str, base: str, ref: str) -> PatchSeries:
        host = Path(repo)
        base_sha = run_git(
            host, "rev-parse", "--verify", base, stage="collect"
        ).stdout.strip()
        result = run_git(
            host, "format-patch", "--stdout", f"{base_sha}..{ref}", stage="collect"
        )
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
    check: bool = True,
    env: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    argv = ["git", *args]
    result = await sandbox.exec(argv, env=env, capture=True)
    if check and result.exit_code != 0:
        raise StageError(
            "collect",
            CommandFailed(
                argv=tuple(argv),
                exit_code=result.exit_code,
                stderr_tail=bound_tail(result.stderr),
            ),
        )
    return result.exit_code, result.stdout, result.stderr


@dataclass(frozen=True, slots=True)
class CollectResult:
    series_meta: Series
    patch_series: PatchSeries
    squashed: bool = False


async def collect(
    sandbox: Sandbox,
    workspace: Workspace,
    *,
    salvage: bool = True,
) -> CollectResult:
    """Salvage, check linearity, and emit a ``PatchSeries`` via format-patch."""
    salvaged = False
    if salvage:
        _, status, _ = await _sandbox_git(sandbox, "status", "--porcelain")
        if status.strip():
            await _salvage(sandbox, workspace)
            salvaged = True

    base = workspace.base_sha
    _, merges, _ = await _sandbox_git(sandbox, "rev-list", "--merges", f"{base}..HEAD")
    ancestor_code, _, _ = await _sandbox_git(
        sandbox, "merge-base", "--is-ancestor", base, "HEAD", check=False
    )
    nonlinear = bool(merges.strip()) or ancestor_code != 0

    if nonlinear:
        patch_series = await _squash_series(sandbox, workspace)
        return CollectResult(
            series_meta=Series(commits=patch_series.commits, salvaged=salvaged),
            patch_series=patch_series,
            squashed=True,
        )

    _, stdout, _ = await _sandbox_git(
        sandbox, "format-patch", "--stdout", f"{base}..HEAD"
    )
    patch_series = PatchSeries.from_format_patch(base, stdout)
    return CollectResult(
        series_meta=Series(commits=patch_series.commits, salvaged=salvaged),
        patch_series=patch_series,
        squashed=False,
    )


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


async def _squash_series(sandbox: Sandbox, workspace: Workspace) -> PatchSeries:
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
                "collect",
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
    return PatchSeries.from_format_patch(base, stdout)
