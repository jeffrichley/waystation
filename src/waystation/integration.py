"""Integration: land a PatchSeries on the host repo without a worktree."""

from __future__ import annotations

import asyncio
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from waystation._git import decode, encode, run_git
from waystation.collect import PatchSeries
from waystation.errors import PreflightError, StageError
from waystation.results import (
    CommandFailed,
    Conflict,
    FailedPatch,
    IntegrationReport,
    Refused,
)
from waystation.tails import bound_tail

__all__ = [
    "Conflict",
    "FailedPatch",
    "GitRepo",
    "Integration",
    "IntegrationReport",
    "IntegrationStrategy",
    "PatchSeries",
    "integrate",
]

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
        return _repo_git(self.path, *args, env=env)


def _repo_git(repo: Path, *args: str, env: Mapping[str, str] | None = None) -> str:
    return run_git(repo, *args, stage="integrate", env=env).stdout.strip()


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


def preserve_series(
    repo: Path | str | GitRepo,
    *,
    branch: str,
    series: PatchSeries,
) -> str:
    """Keep ``series`` on ``branch``, created at the series' base.

    Landing at the base can never conflict. Leaves the host working tree,
    index, and HEAD untouched. Returns ``branch``.
    """
    if series.patches:
        git_repo = repo if isinstance(repo, GitRepo) else GitRepo.open(repo)
        commits = _materialize_at_base(git_repo, series)
        git_repo.git("update-ref", f"refs/heads/{branch}", commits[-1])
    return branch


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
        current = _read_ref(repo, ref)
        target_before = current if current is not None else base

        if not series.patches:
            return self._report(target_before, current)
        holder = _worktree_holding(repo, ref)
        if holder is not None:
            # update-ref would move the branch out from under that checkout,
            # leaving its index and files describing the old tip (ADR-0020).
            raise StageError(
                "integrate",
                Refused(
                    reason="target_checked_out",
                    detail=f"{self.target} is checked out in {holder}",
                ),
            )

        commits = _materialize_at_base(repo, series)
        # One attempt with the configured mechanism: an apply that conflicts
        # never falls back to merge (ADR-0015).
        if self.mechanism == "apply":
            landing = _apply_onto(repo, tip=target_before, commits=commits)
        else:
            landing = _merge_onto(
                repo, tip=target_before, base=base, series_tip=commits[-1]
            )
        if isinstance(landing, Conflict):
            # Nothing but unreferenced objects was written (ADR-0020).
            return self._report(target_before, None, conflict=landing)
        tip, landed = landing

        try:
            # Compare-and-swap: only moves the target if it is still `current`.
            repo.git("update-ref", ref, tip, current or _ZERO)
        except StageError as exc:
            # A refusal is only ours to give if we saw the target move; any
            # other failed swap is git's, reported as it came (ADR-0016).
            if _read_ref(repo, ref) == current:
                raise
            raise StageError(
                "integrate",
                Refused(
                    reason="target_moved",
                    detail=f"{self.target} moved while the series was landing",
                ),
            ) from exc
        return self._report(target_before, tip, landed)

    def _report(
        self,
        before: str,
        after: str | None,
        landed: tuple[str, ...] = (),
        *,
        conflict: Conflict | None = None,
    ) -> IntegrationReport:
        return IntegrationReport(
            strategy="Integration",
            target=self.target,
            mechanism=self.mechanism,
            target_before=before,
            target_after=after,
            landed=landed,
            conflict=conflict,
        )


def _read_ref(repo: GitRepo, ref: str) -> str | None:
    """The commit ``ref`` points at, or ``None`` if there is no such ref.

    Always a full ref name: resolving a bare one lets a tag of the same name win.
    """
    shown = run_git(
        repo.path,
        "rev-parse",
        "--verify",
        "--quiet",
        ref,
        stage="integrate",
        check=False,
    )
    return shown.stdout.strip() if shown.returncode == 0 else None


def _worktree_holding(repo: GitRepo, ref: str) -> str | None:
    """The worktree with ``ref`` checked out, the main one included, if any."""
    listing = repo.git("worktree", "list", "--porcelain", "-z")
    worktree: str | None = None
    for field in listing.split("\0"):
        if field.startswith("worktree "):
            worktree = field.removeprefix("worktree ")
        elif field == f"branch {ref}":
            return worktree
    return None


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
        repo.git("read-tree", series.base_sha, env=env)
        parent = series.base_sha
        commits: list[str] = []
        for patch_text in series.patches:
            parent = _commit_patch(
                repo,
                patch_text=patch_text,
                parent=parent,
                committer_name=name,
                committer_email=email,
                env=env,
            )
            commits.append(parent)
        return commits
    finally:
        index.unlink(missing_ok=True)


_SUBJECT_PATCH_PREFIX = re.compile(r"^\[PATCH(?:\s+\d+/\d+)?\]\s*")


def _commit_patch(
    repo: GitRepo,
    *,
    patch_text: str,
    parent: str,
    committer_name: str,
    committer_email: str,
    env: dict[str, str],
) -> str:
    with tempfile.TemporaryDirectory(prefix="waystation-patch-") as tmp:
        msg_file = Path(tmp) / "MSG"
        diff_file = Path(tmp) / "DIFF"
        mail = run_git(
            repo.path,
            "mailinfo",
            str(msg_file),
            str(diff_file),
            stage="integrate",
            stdin=encode(patch_text),
        )
        author_name, author_email, subject = _parse_mailinfo(mail.stdout)
        subject = _SUBJECT_PATCH_PREFIX.sub("", subject).strip() or "commit"
        body = decode(msg_file.read_bytes())
        message = subject if not body.strip() else f"{subject}\n\n{body}"
        msg_file.write_bytes(encode(message))

        author_date = _parse_date(patch_text)
        if diff_file.stat().st_size > 0:
            repo.git("apply", "--cached", str(diff_file), env=env)

        tree = repo.git("write-tree", env=env)
        commit_env = {
            **env,
            "GIT_AUTHOR_NAME": author_name or committer_name,
            "GIT_AUTHOR_EMAIL": author_email or committer_email,
            "GIT_COMMITTER_NAME": committer_name,
            "GIT_COMMITTER_EMAIL": committer_email,
        }
        if author_date:
            commit_env["GIT_AUTHOR_DATE"] = author_date
        return repo.git(
            "commit-tree", tree, "-p", parent, "-F", str(msg_file), env=commit_env
        )


def _parse_mailinfo(stdout: str) -> tuple[str, str, str]:
    author = ""
    email = ""
    subject = ""
    for line in stdout.splitlines():
        if line.startswith("Author: "):
            author = line[len("Author: ") :].strip()
        elif line.startswith("Email: "):
            email = line[len("Email: ") :].strip()
        elif line.startswith("Subject: "):
            subject = line[len("Subject: ") :].strip()
    return author, email, subject


def _parse_date(patch_text: str) -> str | None:
    for line in patch_text.splitlines():
        if line.startswith("Date: "):
            return line[len("Date: ") :].strip()
        if line.startswith("---"):
            break
    return None


def _message_file(message: str) -> str:
    """Write a commit message to a temp file, bytes as-is (no CRLF on Windows)."""
    with tempfile.NamedTemporaryFile(delete=False, prefix="waystation-msg-") as fh:
        fh.write(encode(message))
        return fh.name


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


type _Landing = tuple[str, tuple[str, ...]] | Conflict
"""The new tip and the commits that landed on it, or where the replay stopped."""


def _apply_onto(repo: GitRepo, *, tip: str, commits: list[str]) -> _Landing:
    name, email = _committer(repo)
    landed: list[str] = []
    for index, commit in enumerate(commits):
        parent = repo.git("rev-parse", f"{commit}^")
        tree = _merge_tree(repo, merge_base=parent, tip=tip, other=commit)
        if isinstance(tree, Conflict):
            subject = repo.git("log", "-1", "--format=%s", commit)
            return replace(tree, failed_patch=FailedPatch(index, subject))
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
        msg_path = _message_file(message)
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
    return tip, tuple(landed)


def _merge_onto(repo: GitRepo, *, tip: str, base: str, series_tip: str) -> _Landing:
    name, email = _committer(repo)
    tree = _merge_tree(repo, merge_base=base, tip=tip, other=series_tip)
    if isinstance(tree, Conflict):
        return tree
    tip_tree = repo.git("rev-parse", f"{tip}^{{tree}}")
    if tree == tip_tree:
        return tip, ()

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
    msg_path = _message_file(message)
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
    return new, (new,)


def _merge_tree(
    repo: GitRepo, *, merge_base: str, tip: str, other: str
) -> str | Conflict:
    """The merged tree's sha, or the paths that conflict; writes no ref, index or file.

    ``-z`` keeps a path with a space or a quote exactly as git stored it.
    """
    merged = run_git(
        repo.path,
        "merge-tree",
        "--write-tree",
        "--name-only",
        "--no-messages",
        "-z",
        f"--merge-base={merge_base}",
        tip,
        other,
        stage="integrate",
        check=False,
    )
    tree, *paths = merged.stdout.split("\0")
    if merged.returncode == 0:
        return tree
    # Exit 1 means conflicts only when a tree came with it; git also exits 1,
    # with nothing on stdout, when it cannot read its inputs.
    if merged.returncode == 1 and tree:
        return Conflict(paths=tuple(path for path in paths if path))
    raise StageError(
        "integrate",
        CommandFailed(
            argv=tuple(merged.args),
            exit_code=merged.returncode,
            stderr_tail=bound_tail(merged.stderr),
        ),
    )
