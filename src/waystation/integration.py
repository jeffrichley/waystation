"""Integration: land a PatchSeries on the host repo without a worktree."""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import tempfile
from collections.abc import AsyncIterator, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable
from weakref import WeakKeyDictionary

from waystation._cancellation import committed
from waystation._git import decode, encode, git_identity, run_git
from waystation.collect import PatchSeries
from waystation.errors import StageError
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
    "GitResult",
    "Integration",
    "IntegrationReport",
    "IntegrationStrategy",
    "PatchSeries",
    "integrate",
]

Mechanism = Literal["apply", "merge"]

# One lock per git common dir, per event loop. An asyncio.Lock binds to the
# loop that first contends for it, so a process running a second loop — a
# service calling asyncio.run per request — would fail its next contended
# landing; keying by the running loop gives each one its own table, and the
# weak keys let a loop that is gone take its locks with it (ADR-0033).
_locks: WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Lock]] = (
    WeakKeyDictionary()
)
# The repos this task already holds, so an entry point called from inside
# another is reentrant instead of deadlocking with no error at all.
_held: ContextVar[frozenset[str]] = ContextVar(
    "waystation_landing", default=frozenset()
)
_ZERO = "0" * 40


@dataclass(frozen=True, slots=True)
class GitResult:
    """What a git command left behind: its exit code and both its streams.

    ``stdout`` and ``stderr`` are text decoded with surrogateescape, never
    newline-translated — a patch through a text pipe on Windows comes out
    changed on every line (ADR-0030).
    """

    exit_code: int
    stdout: str
    stderr: str


# Git commands that move a ref. Once one has started it runs to its end, and a
# cancellation is raised after it, so a target is moved or left alone and never
# half-moved. A table here, never a flag a call site can forget (ADR-0027).
_COMMIT_POINTS = frozenset({"update-ref"})


@dataclass(frozen=True, slots=True)
class GitRepo:
    """Host repo handle: path, common dir, and a git runner.

    This is the only way into host git, for the shipped strategy and a user's
    alike: ``git`` for the calls that want a sha and raise, ``run`` for the
    calls that read an exit code or feed something in (ADR-0033).
    """

    path: Path
    common_dir: Path

    @classmethod
    async def open(cls, repo: Path | str) -> GitRepo:
        path = Path(repo).resolve()
        common_raw = await _repo_git(path, "rev-parse", "--git-common-dir", check=True)
        common = Path(common_raw.stdout.strip())
        if not common.is_absolute():
            common = (path / common).resolve()
        return cls(path=path, common_dir=common)

    async def git(self, *args: str, env: Mapping[str, str] | None = None) -> str:
        """Run git in this repo; its stdout, stripped. Raises if git failed.

        The ergonomic call, for the many that want a sha. Reach for ``run``
        when the exit code is the answer, or something goes in on stdin.
        """
        return (await self.run(*args, check=True, env=env)).stdout.strip()

    async def run(
        self,
        *args: str,
        check: bool = False,
        stdin: bytes | None = None,
        env: Mapping[str, str] | None = None,
    ) -> GitResult:
        """Run git in this repo and hand back its exit code and both streams.

        ``check=True`` raises ``StageError`` on a non-zero exit instead, as
        ``git`` does. ``stdin`` is bytes, and the streams come back as text
        that was never newline-translated: a patch survives either way on a
        Windows host, which a text-mode pipe would not (ADR-0030).

        Cancelling it kills git and all it started (ADR-0023) — except a
        command that moves a ref, which finishes first, the cancellation
        raised after it (ADR-0027).
        """
        running = _repo_git(self.path, *args, check=check, stdin=stdin, env=env)
        if args[:1] and args[0] in _COMMIT_POINTS:
            return await committed(running)
        return await running


async def _repo_git(
    repo: Path,
    *args: str,
    check: bool,
    stdin: bytes | None = None,
    env: Mapping[str, str] | None = None,
) -> GitResult:
    # stage="integrate" whoever runs it: #70 moves attribution to each
    # primitive's edge, and the two tickets must not rewrite the same lines.
    done = await run_git(
        repo, *args, stage="integrate", check=check, stdin=stdin, env=env
    )
    # Never stripped: `merge-tree -z` splits its output on NULs, and the
    # last field is empty. `git` strips for the many callers that want a sha.
    return GitResult(exit_code=done.returncode, stdout=done.stdout, stderr=done.stderr)


@runtime_checkable
class IntegrationStrategy(Protocol):
    async def integrate(
        self, repo: GitRepo, series: PatchSeries
    ) -> IntegrationReport: ...


@contextlib.asynccontextmanager
async def _serialized(common_dir: Path) -> AsyncIterator[None]:
    """Hold this repo's lock for the block; reentrant within one task.

    Reentrancy is not politeness: with serialization at the entry points, a
    strategy preserving a series it could not land calls one entry point from
    inside another, and would otherwise hang with no error (ADR-0033).
    """
    key = str(common_dir.resolve())
    holding = _held.get()
    if key in holding:
        yield
        return
    per_loop = _locks.setdefault(asyncio.get_running_loop(), {})
    lock = per_loop.get(key)
    if lock is None:
        lock = per_loop[key] = asyncio.Lock()
    # A bound or cancellation kills the git under way before it leaves the
    # lock, so the lock is never free while a write is still going on (#57).
    async with lock:
        token = _held.set(holding | {key})
        try:
            yield
        finally:
            _held.reset(token)


async def integrate(
    repo: Path | str | GitRepo,
    series: PatchSeries,
    strategy: IntegrationStrategy,
) -> IntegrationReport:
    """Land ``series`` with ``strategy``, serialized per git common dir.

    This and ``preserve_series`` are the serialized ways in. Calling a
    strategy's own ``integrate()`` is not: it is like running git yourself,
    and two of them against one repo race each other (ADR-0005).

    Cancelling it kills the strategy's git and all it started (ADR-0023),
    except an ``update-ref`` already under way: that finishes first, so the
    target is moved or not, never half-moved, and the cancellation is still
    raised (ADR-0027).
    """
    git_repo = repo if isinstance(repo, GitRepo) else await GitRepo.open(repo)
    async with _serialized(git_repo.common_dir):
        return await strategy.integrate(git_repo, series)


async def preserve_series(
    repo: Path | str | GitRepo,
    *,
    branch: str,
    series: PatchSeries,
) -> str:
    """Keep ``series`` on ``branch``, created at the series' base.

    Landing at the base can never conflict. Leaves the host working tree,
    index, and HEAD untouched. Returns ``branch``.

    Serialized with landings on the same repo: it writes objects and a ref
    into the host, so it takes the same lock ``integrate`` does (ADR-0033).
    """
    if series.patches:
        git_repo = repo if isinstance(repo, GitRepo) else await GitRepo.open(repo)
        async with _serialized(git_repo.common_dir):
            commits = await _materialize_at_base(git_repo, series)
            await git_repo.git("update-ref", f"refs/heads/{branch}", commits[-1])
    return branch


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
        return await self._land(repo, series)

    async def _land(self, repo: GitRepo, series: PatchSeries) -> IntegrationReport:
        ref = f"refs/heads/{self.target}"
        base = series.base_sha
        current = await _read_ref(repo, ref)
        target_before = current if current is not None else base

        holder = await _worktree_holding(repo, ref)
        if holder is not None:
            # update-ref would move the branch out from under that checkout,
            # leaving its index and files describing the old tip (ADR-0020).
            # Refused even when nothing would land, as `git branch -f` refuses
            # a no-op: the target is wrong whatever this series holds.
            raise StageError(
                "integrate",
                Refused(
                    reason="target_checked_out",
                    detail=f"{self.target} is checked out in {holder}",
                ),
            )
        if not series.patches:
            return self._report(target_before, current)

        commits = await _materialize_at_base(repo, series)
        # One attempt with the configured mechanism: an apply that conflicts
        # never falls back to merge (ADR-0015).
        if self.mechanism == "apply":
            landing = await _apply_onto(repo, tip=target_before, commits=commits)
        else:
            landing = await _merge_onto(
                repo, tip=target_before, base=base, series_tip=commits[-1]
            )
        if isinstance(landing, Conflict):
            # Nothing but unreferenced objects was written (ADR-0020).
            return self._report(target_before, None, conflict=landing)
        tip, landed = landing

        try:
            # Compare-and-swap: only moves the target if it is still `current`.
            await repo.git("update-ref", ref, tip, current or _ZERO)
        except StageError as exc:
            # A refusal is only ours to give if we saw the target move; any
            # other failed swap is git's, reported as it came (ADR-0016).
            if await _read_ref(repo, ref) == current:
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


async def _read_ref(repo: GitRepo, ref: str) -> str | None:
    """The commit ``ref`` points at, or ``None`` if there is no such ref.

    Always a full ref name: resolving a bare one lets a tag of the same name win.
    """
    shown = await repo.run("rev-parse", "--verify", "--quiet", ref)
    return shown.stdout.strip() if shown.exit_code == 0 else None


async def _worktree_holding(repo: GitRepo, ref: str) -> str | None:
    """The worktree using ``ref``, the main one included, if any.

    Using means what it means to ``git branch -f``: checked out, or the branch
    a rebase or bisect there will return to. A rebase detaches HEAD, but
    finishing it writes that branch over whatever landed meanwhile.
    """
    listing = await repo.git("worktree", "list", "--porcelain", "-z")
    for index, record in enumerate(listing.split("\0\0")):
        fields = record.strip("\0").split("\0")
        if not fields[0].startswith("worktree "):
            continue
        worktree = fields[0].removeprefix("worktree ")
        if f"branch {ref}" in fields:
            return worktree
        git_dir = repo.common_dir if index == 0 else _linked_git_dir(worktree)
        if git_dir is not None and ref in _returning_to(git_dir):
            return worktree
    return None


def _linked_git_dir(worktree: str) -> Path | None:
    """A linked worktree's private git dir, read from its ``.git`` file."""
    try:
        pointer = decode((Path(worktree) / ".git").read_bytes())
    except OSError:
        return None  # a worktree whose directory is gone has nothing in flight
    git_dir = Path(pointer.removeprefix("gitdir:").strip())
    return git_dir if git_dir.is_absolute() else Path(worktree) / git_dir


# Where a paused rebase or bisect keeps the branch it will go back to: git's
# own is_worktree_being_rebased / is_worktree_being_bisected read the same.
_REBASE_HEAD_NAMES = ("rebase-merge/head-name", "rebase-apply/head-name")
_BISECT_START = "BISECT_START"


def _returning_to(git_dir: Path) -> set[str]:
    """The refs a rebase or bisect paused in ``git_dir`` will return to."""
    refs: set[str] = set()
    for name in _REBASE_HEAD_NAMES:
        head_name = _read_state(git_dir / name)
        if head_name is not None:
            refs.add(head_name)
    bisected_from = _read_state(git_dir / _BISECT_START)
    if bisected_from is not None:
        refs.add(f"refs/heads/{bisected_from}")
    return refs


def _read_state(path: Path) -> str | None:
    try:
        return decode(path.read_bytes()).strip() or None
    except OSError:
        return None


async def _committer(repo: GitRepo) -> tuple[str, str]:
    return await git_identity(repo.path, stage="integrate")


async def _materialize_at_base(repo: GitRepo, series: PatchSeries) -> list[str]:
    """Rebuild each patch as a commit atop ``series.base_sha``; return shas."""
    name, email = await _committer(repo)
    fd, index_path = tempfile.mkstemp(prefix="waystation-idx-")
    os.close(fd)
    index = Path(index_path)
    try:
        env = {**os.environ, "GIT_INDEX_FILE": str(index)}
        await repo.git("read-tree", series.base_sha, env=env)
        parent = series.base_sha
        commits: list[str] = []
        for patch_text in series.patches:
            parent = await _commit_patch(
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


async def _commit_patch(
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
        mail = await repo.run(
            "mailinfo",
            str(msg_file),
            str(diff_file),
            stdin=encode(patch_text),
            check=True,
        )
        author_name, author_email, subject = _parse_mailinfo(mail.stdout)
        subject = _SUBJECT_PATCH_PREFIX.sub("", subject).strip() or "commit"
        body = decode(msg_file.read_bytes())
        message = subject if not body.strip() else f"{subject}\n\n{body}"
        msg_file.write_bytes(encode(message))

        author_date = _parse_date(patch_text)
        if diff_file.stat().st_size > 0:
            await repo.git("apply", "--cached", str(diff_file), env=env)

        tree = await repo.git("write-tree", env=env)
        commit_env = {
            **env,
            "GIT_AUTHOR_NAME": author_name or committer_name,
            "GIT_AUTHOR_EMAIL": author_email or committer_email,
            "GIT_COMMITTER_NAME": committer_name,
            "GIT_COMMITTER_EMAIL": committer_email,
        }
        if author_date:
            commit_env["GIT_AUTHOR_DATE"] = author_date
        return await repo.git(
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


async def _commit_meta(repo: GitRepo, sha: str) -> tuple[str, str, str, str]:
    raw = await repo.git("log", "-1", "--format=%an%n%ae%n%aI%n%B", sha)
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


async def _apply_onto(repo: GitRepo, *, tip: str, commits: list[str]) -> _Landing:
    name, email = await _committer(repo)
    landed: list[str] = []
    for index, commit in enumerate(commits):
        parent = await repo.git("rev-parse", f"{commit}^")
        tree = await _merge_tree(repo, merge_base=parent, tip=tip, other=commit)
        if isinstance(tree, Conflict):
            subject = await repo.git("log", "-1", "--format=%s", commit)
            return replace(tree, failed_patch=FailedPatch(index, subject))
        tip_tree = await repo.git("rev-parse", f"{tip}^{{tree}}")
        if tree == tip_tree:
            continue
        author, author_email, author_date, message = await _commit_meta(repo, commit)
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
            new = await repo.git(
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


async def _merge_onto(
    repo: GitRepo, *, tip: str, base: str, series_tip: str
) -> _Landing:
    name, email = await _committer(repo)
    tree = await _merge_tree(repo, merge_base=base, tip=tip, other=series_tip)
    if isinstance(tree, Conflict):
        return tree
    tip_tree = await repo.git("rev-parse", f"{tip}^{{tree}}")
    if tree == tip_tree:
        return tip, ()

    author, author_email, author_date, _msg = await _commit_meta(repo, series_tip)
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
        new = await repo.git(
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


async def _merge_tree(
    repo: GitRepo, *, merge_base: str, tip: str, other: str
) -> str | Conflict:
    """The merged tree's sha, or the paths that conflict; writes no ref, index or file.

    ``-z`` keeps a path with a space or a quote exactly as git stored it.
    """
    args = (
        "merge-tree",
        "--write-tree",
        "--name-only",
        "--no-messages",
        "-z",
        f"--merge-base={merge_base}",
        tip,
        other,
    )
    # `run`, not `git`: the exit code is the answer, and stripping stdout
    # would eat the NULs `-z` splits on.
    merged = await repo.run(*args)
    tree, *paths = merged.stdout.split("\0")
    if merged.exit_code == 0:
        return tree.strip()
    # Exit 1 means conflicts only when a tree came with it; git also exits 1,
    # with nothing on stdout, when it cannot read its inputs.
    if merged.exit_code == 1 and tree:
        return Conflict(paths=tuple(path for path in paths if path))
    raise StageError(
        "integrate",
        CommandFailed(
            argv=("git", "-C", str(repo.path), *args),
            exit_code=merged.exit_code,
            stderr_tail=bound_tail(merged.stderr),
        ),
    )
