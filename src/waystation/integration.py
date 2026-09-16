"""Integration: land a PatchSeries onto a named branch without a worktree."""

from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from waystation.collect import PatchSeries, _commit_patch, _host_git
from waystation.errors import PreflightError, StageError
from waystation.results import CommandFailed, IntegrationReport, Refused
from waystation.tails import bound_tail

Mechanism = Literal["apply", "merge"]

_locks: dict[str, asyncio.Lock] = {}
_ZERO = "0" * 40


@dataclass(frozen=True, slots=True)
class GitRepo:
    """Host repo handle: path, common dir, and a git runner."""

    path: Path
    common_dir: Path

    @classmethod
    def open(cls, repo: Path | str) -> GitRepo:
        path = Path(repo).resolve()
        common_raw = _repo_git(path, "rev-parse", "--git-common-dir")
        common = Path(common_raw)
        if not common.is_absolute():
            common = (path / common).resolve()
        return cls(path=path, common_dir=common)

    def git(self, *args: str, env: Mapping[str, str] | None = None) -> str:
        return _repo_git(self.path, *args, env=dict(env) if env else None)


def _repo_git(
    repo: Path,
    *args: str,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> str:
    argv = ["git", "-C", str(repo), *args]
    result = subprocess.run(
        argv,
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    if check and result.returncode != 0:
        raise StageError(
            "integrate",
            CommandFailed(
                argv=tuple(argv),
                exit_code=result.returncode,
                stderr_tail=bound_tail(result.stderr),
            ),
        )
    return result.stdout.strip()


@runtime_checkable
class IntegrationStrategy(Protocol):
    async def integrate(
        self, repo: GitRepo, series: PatchSeries
    ) -> IntegrationReport: ...


def _lock_for(common_dir: Path) -> asyncio.Lock:
    key = str(common_dir.resolve())
    lock = _locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _locks[key] = lock
    return lock


async def integrate(
    repo: Path | str | GitRepo,
    series: PatchSeries,
    strategy: IntegrationStrategy,
) -> IntegrationReport:
    """Land ``series`` with ``strategy``, serialized per git common dir."""
    git_repo = repo if isinstance(repo, GitRepo) else GitRepo.open(repo)
    _require_git_240(git_repo)
    async with _lock_for(git_repo.common_dir):
        return await strategy.integrate(git_repo, series)


def _require_git_240(repo: GitRepo) -> None:
    raw = repo.git("--version")  # e.g. "git version 2.43.0.windows.1"
    parts = raw.replace("git version ", "").split(".")
    try:
        major = int(parts[0])
        minor = int(parts[1])
    except (IndexError, ValueError) as exc:
        raise PreflightError(f"could not parse git version: {raw!r}") from exc
    if (major, minor) < (2, 40):
        raise PreflightError(f"host git must be ≥ 2.40 (got {raw})")


@dataclass(frozen=True, slots=True)
class Integration:
    """Shipped strategy: land onto a named branch with apply or merge."""

    target: str
    mechanism: Mechanism = "apply"

    async def integrate(self, repo: GitRepo, series: PatchSeries) -> IntegrationReport:
        if self.target in ("HEAD", "head"):
            raise StageError(
                "integrate",
                Refused(
                    reason="dirty_tree",
                    detail="HEAD target is not supported yet; use a named branch",
                ),
            )
        return await asyncio.to_thread(self._integrate_sync, repo, series)

    def _integrate_sync(self, repo: GitRepo, series: PatchSeries) -> IntegrationReport:
        ref = f"refs/heads/{self.target}"
        base = series.base_sha
        exists = _ref_exists(repo, ref)
        target_before = repo.git("rev-parse", self.target) if exists else base

        if not series.patches:
            return IntegrationReport(
                strategy="Integration",
                target=self.target,
                mechanism=self.mechanism,
                target_before=target_before,
                target_after=target_before if exists else None,
                landed=(),
            )

        commits = _materialize_at_base(repo, series)
        if self.mechanism == "apply":
            tip, landed = _apply_onto(repo, tip=target_before, commits=commits)
        else:
            tip, landed = _merge_onto(
                repo, tip=target_before, base=base, series_tip=commits[-1]
            )

        old = target_before if exists else _ZERO
        try:
            repo.git("update-ref", ref, tip, old)
        except StageError as exc:
            if isinstance(exc.failure, CommandFailed):
                raise StageError(
                    "integrate",
                    Refused(
                        reason="target_moved",
                        detail=f"{self.target} moved during integrate",
                    ),
                ) from exc
            raise
        return IntegrationReport(
            strategy="Integration",
            target=self.target,
            mechanism=self.mechanism,
            target_before=target_before,
            target_after=tip,
            landed=tuple(landed),
        )


def _ref_exists(repo: GitRepo, ref: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(repo.path), "show-ref", "--verify", "--quiet", ref],
        check=False,
        capture_output=True,
    )
    return result.returncode == 0


def _committer(repo: GitRepo) -> tuple[str, str]:
    try:
        name = repo.git("config", "--get", "user.name")
        email = repo.git("config", "--get", "user.email")
    except StageError as exc:
        raise StageError(
            "integrate",
            Refused(
                reason="no_git_identity",
                detail="host repo has no git identity (user.name / user.email)",
            ),
        ) from exc
    if not name or not email:
        raise StageError(
            "integrate",
            Refused(
                reason="no_git_identity",
                detail="host repo has no git identity (user.name / user.email)",
            ),
        )
    return name, email


def _materialize_at_base(repo: GitRepo, series: PatchSeries) -> list[str]:
    """Rebuild each patch as a commit atop ``series.base_sha``; return shas."""
    name, email = _committer(repo)
    fd, index_path = tempfile.mkstemp(prefix="waystation-idx-")
    os.close(fd)
    index = Path(index_path)
    try:
        env = {**os.environ, "GIT_INDEX_FILE": str(index)}
        _host_git(repo.path, "read-tree", series.base_sha, env=env, stage="integrate")
        parent = series.base_sha
        commits: list[str] = []
        for patch_text in series.patches:
            parent = _commit_patch(
                repo.path,
                patch_text=patch_text,
                parent=parent,
                committer_name=name,
                committer_email=email,
                env=env,
                stage="integrate",
            )
            commits.append(parent)
        return commits
    finally:
        index.unlink(missing_ok=True)


def _commit_meta(repo: GitRepo, sha: str) -> tuple[str, str, str, str]:
    raw = repo.git("log", "-1", "--format=%an%n%ae%n%aI%n%B", sha)
    lines = raw.splitlines()
    author = lines[0] if lines else ""
    email = lines[1] if len(lines) > 1 else ""
    date = lines[2] if len(lines) > 2 else ""
    message = "\n".join(lines[3:]) if len(lines) > 3 else "commit"
    if message.endswith("\n"):
        message = message[:-1]
    return author, email, date, message


def _apply_onto(
    repo: GitRepo, *, tip: str, commits: list[str]
) -> tuple[str, list[str]]:
    name, email = _committer(repo)
    landed: list[str] = []
    for commit in commits:
        parent = repo.git("rev-parse", f"{commit}^")
        tree_out, code, err = _merge_tree(
            repo, merge_base=parent, tip=tip, other=commit
        )
        if code != 0:
            raise StageError(
                "integrate",
                CommandFailed(
                    argv=("git", "merge-tree", "--write-tree"),
                    exit_code=code,
                    stderr_tail=bound_tail(err or tree_out),
                ),
            )
        tree = tree_out.splitlines()[0].strip()
        tip_tree = repo.git("rev-parse", f"{tip}^{{tree}}")
        if tree == tip_tree:
            continue
        author, author_email, author_date, message = _commit_meta(repo, commit)
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": author or name,
            "GIT_AUTHOR_EMAIL": author_email or email,
            "GIT_COMMITTER_NAME": name,
            "GIT_COMMITTER_EMAIL": email,
        }
        if author_date:
            env["GIT_AUTHOR_DATE"] = author_date
        with tempfile.NamedTemporaryFile(
            "w", delete=False, prefix="waystation-msg-", encoding="utf-8"
        ) as fh:
            fh.write(message)
            msg_path = fh.name
        try:
            new = repo.git(
                "commit-tree",
                tree,
                "-p",
                tip,
                "-F",
                msg_path,
                env=env,
            )
        finally:
            Path(msg_path).unlink(missing_ok=True)
        tip = new
        landed.append(new)
    return tip, landed


def _merge_onto(
    repo: GitRepo, *, tip: str, base: str, series_tip: str
) -> tuple[str, list[str]]:
    name, email = _committer(repo)
    tree_out, code, err = _merge_tree(repo, merge_base=base, tip=tip, other=series_tip)
    if code != 0:
        raise StageError(
            "integrate",
            CommandFailed(
                argv=("git", "merge-tree", "--write-tree"),
                exit_code=code,
                stderr_tail=bound_tail(err or tree_out),
            ),
        )
    tree = tree_out.splitlines()[0].strip()
    tip_tree = repo.git("rev-parse", f"{tip}^{{tree}}")
    if tree == tip_tree:
        return tip, []

    author, author_email, author_date, _msg = _commit_meta(repo, series_tip)
    message = f"Merge {series_tip[:8]} into branch"
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": author or name,
        "GIT_AUTHOR_EMAIL": author_email or email,
        "GIT_COMMITTER_NAME": name,
        "GIT_COMMITTER_EMAIL": email,
    }
    if author_date:
        env["GIT_AUTHOR_DATE"] = author_date
    with tempfile.NamedTemporaryFile(
        "w", delete=False, prefix="waystation-msg-", encoding="utf-8"
    ) as fh:
        fh.write(message)
        msg_path = fh.name
    try:
        new = repo.git(
            "commit-tree",
            tree,
            "-p",
            tip,
            "-p",
            series_tip,
            "-F",
            msg_path,
            env=env,
        )
    finally:
        Path(msg_path).unlink(missing_ok=True)
    return new, [new]


def _merge_tree(
    repo: GitRepo, *, merge_base: str, tip: str, other: str
) -> tuple[str, int, str]:
    argv = [
        "git",
        "-C",
        str(repo.path),
        "merge-tree",
        "--write-tree",
        f"--merge-base={merge_base}",
        tip,
        other,
    ]
    result = subprocess.run(argv, check=False, capture_output=True, text=True)
    return result.stdout, result.returncode, result.stderr
