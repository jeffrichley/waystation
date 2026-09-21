"""Integration: land a PatchSeries on the host repo without a worktree.

Two strategies ship, ``Integration`` (apply or merge) and ``Squash`` (one
commit), and both are built from ``GitRepo``'s public landing steps alone, as
a user's strategy would be (ADR-0040).
"""

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
from typing import Any, Literal, Protocol, runtime_checkable
from weakref import WeakKeyDictionary, WeakValueDictionary

from waystation._cancellation import committed
from waystation._git import decode, encode, git_identity, run_git
from waystation.collect import PatchSeries
from waystation.errors import StageError, attributing
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
    "Squash",
    "Target",
    "integrate",
    "preserve_series",
]

Mechanism = Literal["apply", "merge"]

# One lock per git common dir, per event loop. An asyncio.Lock binds to the
# loop that first contends for it, so a process running a second loop — a
# service calling asyncio.run per request — would fail its next contended
# landing; keying by the running loop gives each one its own table (ADR-0033).
#
# Both halves are weak on purpose. A Lock caches the loop it bound to, so a
# table holding its locks strongly would hold the loop through its own weak
# key and nothing would ever be freed. Weak values drop a lock the moment no
# landing is using it, which is exactly when a fresh one would do; whoever
# holds or waits for one keeps it alive meanwhile.
_locks: WeakKeyDictionary[
    asyncio.AbstractEventLoop, WeakValueDictionary[str, asyncio.Lock]
] = WeakKeyDictionary()


@dataclass(frozen=True, slots=True)
class _Holding:
    """The repos ``task`` holds, so an entry point nested in one is reentrant.

    The task is part of it because a child task inherits this context: a
    strategy that spawns its own landing must wait for the lock, not walk
    past it believing its parent's hold is its own.
    """

    task: asyncio.Task[Any] | None
    keys: frozenset[str]


_held: ContextVar[_Holding | None] = ContextVar("waystation_landing", default=None)
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
# `merge` is how a HEAD target moves: a fast-forward of the checkout, which
# a kill would leave with the ref moved and half its files not (ADR-0041).
_COMMIT_POINTS = frozenset({"update-ref", "merge"})

# Git's own options, which come before the subcommand; each takes a value.
_GIT_OPTIONS_WITH_VALUE = frozenset(
    {"-c", "-C", "--git-dir", "--work-tree", "--namespace", "--exec-path"}
)


def _subcommand(args: tuple[str, ...]) -> str | None:
    """The git subcommand in ``args``, past git's own options.

    ``repo.git("-c", alias, "update-ref", ...)`` is a shape callers write,
    and reading only the first argument would lose its commit point — the
    half-moved ref ADR-0027 exists to prevent.
    """
    rest = iter(args)
    for arg in rest:
        if arg in _GIT_OPTIONS_WITH_VALUE:
            next(rest, None)  # skip its value
        elif not arg.startswith("-"):
            return arg
    return None


@dataclass(frozen=True, slots=True)
class Target:
    """A branch a landing is about to move, as it stood when it was read.

    Attributes:
        branch: The branch's short name, as a flow script wrote it — or, for
            a ``HEAD`` target, the branch checked out, and ``"HEAD"`` itself
            when the checkout is detached.
        tip: What a landing builds on: the branch's commit, or the series'
            base when the branch does not exist yet, since landing creates
            it there.
        exists: Whether the branch existed. The swap checks it, so a branch
            someone else created meanwhile is a moved target too.
        head: Whether this is the host's checkout, which ``move_target``
            fast-forwards rather than swapping a ref under it (ADR-0041).
    """

    branch: str
    tip: str
    exists: bool
    head: bool = False

    @property
    def ref(self) -> str:
        """The full ref: a bare name would let a tag of the same name win."""
        if self.head and self.branch == "HEAD":
            return "HEAD"
        return f"refs/heads/{self.branch}"


@dataclass(frozen=True, slots=True)
class GitRepo:
    """Host repo handle: path, common dir, a git runner, and the landing steps.

    This is the only way into host git, for the shipped strategies and a
    user's alike: ``git`` for the calls that want a sha and raise, ``run``
    for the calls that read an exit code or feed something in (ADR-0033).

    The landing steps — ``read_target``, ``commit_series``, ``merge_tree``,
    ``commit_tree`` and ``move_target`` — are what ``Integration`` and
    ``Squash`` are built from, and nothing else is: a user's strategy is
    policy over the same steps, never a rewrite of the plumbing (ADR-0040).
    None of them touches a working tree but ``move_target`` fast-forwarding
    a ``HEAD`` target, after a conflict is already ruled out (ADR-0020,
    ADR-0041). Each raises with no stage, because ``integrate`` names it (ADR-0032).
    """

    path: Path
    common_dir: Path

    @classmethod
    async def open(cls, repo: Path | str) -> GitRepo:
        """Open the repo holding ``repo``, at the top of its working tree.

        The top, not the directory given: git run from a subdirectory
        narrows to it, and ``apply --cached`` there drops every path of a
        patch outside it without a word.
        """
        path = Path(repo).resolve()
        top = await _repo_git(path, "rev-parse", "--show-toplevel", check=False)
        if top.exit_code == 0 and top.stdout.strip():
            path = Path(top.stdout.strip()).resolve()
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
        ``git`` does; the error names no stage, because the primitive that
        ran it does (ADR-0032). ``stdin`` is bytes, and the streams come back as text
        that was never newline-translated: a patch survives either way on a
        Windows host, which a text-mode pipe would not (ADR-0030).

        Cancelling it kills git and all it started (ADR-0023) — except a
        command that moves a ref, which finishes first, the cancellation
        raised after it (ADR-0027).
        """
        running = _repo_git(self.path, *args, check=check, stdin=stdin, env=env)
        if _subcommand(args) in _COMMIT_POINTS:
            return await committed(running)
        return await running

    async def read_target(self, branch: str, *, base: str) -> Target:
        """Read the branch a landing will move, refusing one it must not.

        Args:
            branch: The target branch's short name.
            base: Where the branch lands from if it does not exist yet —
                the series' base.

        ``"HEAD"`` is the host's own checkout: the branch it has out, or the
        detached commit. Here, not in each strategy, so every strategy built
        on these steps lands on HEAD alike (ADR-0040).

        Returns:
            The target as it stands now; ``move_target`` swaps it.

        Raises:
            StageError: ``Refused("target_checked_out")`` when a worktree is
                using the branch, since ``update-ref`` would move it out from
                under that checkout (ADR-0020); ``Refused("dirty_tree")``
                when the target is ``HEAD`` and a tracked file has staged or
                unstaged changes, before anything is written.
        """
        if branch == "HEAD":
            await _refuse_tracked_changes(self)
            on, tip = await _read_head(self)
            return Target(
                branch=on or "HEAD", tip=tip or base, exists=tip is not None, head=True
            )
        ref = f"refs/heads/{branch}"
        holder = await _worktree_holding(self, ref)
        if holder is not None:
            # Refused even when nothing would land, as `git branch -f` refuses
            # a no-op: the target is wrong whatever this series holds.
            raise StageError(
                None,
                Refused(
                    reason="target_checked_out",
                    detail=f"{branch} is checked out in {holder}",
                ),
            )
        current = await _read_ref(self, ref)
        return Target(branch=branch, tip=current or base, exists=current is not None)

    async def commit_series(self, series: PatchSeries) -> tuple[str, ...]:
        """Rebuild each patch as a commit atop the series' base, in order.

        In a temporary index, never a checkout: ``mailinfo``, ``apply
        --cached``, ``write-tree``, ``commit-tree``. It cannot conflict,
        because the patches were cut against that base. Each commit keeps
        its patch's author, author date and message; the committer is the
        host user. Nothing points at them until a ref is moved (ADR-0020).

        Args:
            series: The patches to rebuild.

        Returns:
            The rebuilt commits' shas, oldest first; empty for an empty series.
        """
        name, email = await git_identity(self.path)
        fd, index_path = tempfile.mkstemp(prefix="waystation-idx-")
        os.close(fd)
        index = Path(index_path)
        try:
            env = {**os.environ, "GIT_INDEX_FILE": str(index)}
            await self.git("read-tree", series.base_sha, env=env)
            parent = series.base_sha
            commits: list[str] = []
            for patch_text in series.patches:
                parent = await _commit_patch(
                    self,
                    patch_text=patch_text,
                    parent=parent,
                    committer_name=name,
                    committer_email=email,
                    env=env,
                )
                commits.append(parent)
            return tuple(commits)
        finally:
            index.unlink(missing_ok=True)

    async def merge_tree(self, *, base: str, ours: str, theirs: str) -> str | Conflict:
        """Merge ``theirs`` into ``ours`` as trees, from ``base``.

        One ``merge-tree --write-tree``: no ref, index or file is written,
        so a conflict leaves nothing behind but unreferenced objects.

        Args:
            base: The merge base — a commit's parent to replay that commit,
                the series' base to replay its whole net change.
            ours: The commit to build on, usually the target's tip.
            theirs: The commit whose change comes in.

        Returns:
            The merged tree's sha, or a ``Conflict`` naming the paths that
            conflicted exactly as git stored them. It names no patch: which
            one stopped a replay is the caller's to say.

        Raises:
            StageError: ``CommandFailed`` when git could not merge at all,
                as opposed to finding a conflict.
        """
        args = (
            "merge-tree",
            "--write-tree",
            "--name-only",
            "--no-messages",
            "-z",
            f"--merge-base={base}",
            ours,
            theirs,
        )
        # `run`, not `git`: the exit code is the answer, and stripping stdout
        # would eat the NULs `-z` splits on.
        merged = await self.run(*args)
        tree, *paths = merged.stdout.split("\0")
        if merged.exit_code == 0:
            return tree
        # Exit 1 means conflicts only when a tree came with it; git also exits 1,
        # with nothing on stdout, when it cannot read its inputs.
        if merged.exit_code == 1 and tree:
            return Conflict(paths=tuple(path for path in paths if path))
        raise StageError(
            None,
            CommandFailed(
                argv=("git", "-C", str(self.path), *args),
                exit_code=merged.exit_code,
                stderr_tail=bound_tail(merged.stderr),
            ),
        )

    async def commit_tree(
        self, tree: str, *parents: str, like: str, message: str | None = None
    ) -> str:
        """Commit ``tree`` atop ``parents``, authored as ``like`` was.

        The author and author date are ``like``'s, and so is the message
        unless ``message`` replaces it; the committer is the host user, now
        (ADR-0006, ADR-0020). The message reaches git through a file, as
        bytes, so a Windows host adds no carriage return to it.

        Args:
            tree: The tree to commit — what ``merge_tree`` returned.
            *parents: The new commit's parents, first parent first.
            like: The commit whose authorship, and message, this one carries.
            message: The message to use instead of ``like``'s.

        Returns:
            The new commit's sha. No ref points at it yet.
        """
        name, email = await git_identity(self.path)
        author, author_email, author_date, own = await _commit_meta(self, like)
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": author or name,
            "GIT_AUTHOR_EMAIL": author_email or email,
            "GIT_COMMITTER_NAME": name,
            "GIT_COMMITTER_EMAIL": email,
        }
        if author_date:
            env["GIT_AUTHOR_DATE"] = author_date
        msg_path = _message_file(own if message is None else message)
        try:
            return await self.git(
                "commit-tree",
                tree,
                *(arg for parent in parents for arg in ("-p", parent)),
                "-F",
                msg_path,
                env=env,
            )
        finally:
            Path(msg_path).unlink(missing_ok=True)

    async def move_target(self, target: Target, tip: str) -> None:
        """Move ``target`` to ``tip``, only if it stands where it was read.

        A compare-and-swap ``update-ref``: the per-repo lock is in-process
        only, so something outside the process may have moved the branch
        since ``read_target``. Once started, the swap finishes, even when
        cancelled (ADR-0027).

        A ``HEAD`` target is the checkout, so it moves the way a user's would:
        ``merge --ff-only``, after checking that the checkout still stands
        where it was read, is still clean, and has no file of the user's where
        the landing puts one (ADR-0041).

        Args:
            target: The target as ``read_target`` returned it.
            tip: The commit to move it to.

        Raises:
            StageError: ``Refused("target_moved")`` when a re-read shows the
                branch moved. Any other failed swap is git's own
                ``CommandFailed``, as it came, so a retry keyed on
                ``target_moved`` never spins on a lock (ADR-0016, ADR-0020).
                For ``HEAD``, ``Refused("dirty_tree")`` too, when the
                checkout gained a tracked change or holds an untracked file
                the landing would overwrite.
        """
        if target.head:
            await _fast_forward(self, target, tip)
            return
        before = target.tip if target.exists else None
        try:
            await self.git("update-ref", target.ref, tip, before or _ZERO)
        except StageError as exc:
            # A refusal is only ours to give if we saw the target move.
            if await _read_ref(self, target.ref) == before:
                raise
            raise StageError(
                None,
                Refused(
                    reason="target_moved",
                    detail=f"{target.branch} moved while the series was landing",
                ),
            ) from exc


async def _repo_git(
    repo: Path,
    *args: str,
    check: bool,
    stdin: bytes | None = None,
    env: Mapping[str, str] | None = None,
) -> GitResult:
    # No stage: a GitRepo serves a landing, a preservation and a user's own
    # strategy alike, so the primitive's edge names the stage (ADR-0032).
    done = await run_git(repo, *args, check=check, stdin=stdin, env=env)
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
    running = asyncio.current_task()
    holding = _held.get()
    if holding is None or holding.task is not running:
        # A child task inherits this context, and someone else's hold is not
        # this task's: it waits its turn like any other landing.
        holding = _Holding(running, frozenset())
    if key in holding.keys:
        yield
        return
    per_loop = _locks.setdefault(asyncio.get_running_loop(), WeakValueDictionary())
    lock = per_loop.get(key)
    if lock is None:
        lock = per_loop[key] = asyncio.Lock()
    # `lock` stays a local for the whole block: it is what keeps this repo's
    # entry in the weak table alive while anyone holds or waits for it.
    #
    # A bound or cancellation kills the git under way before it leaves the
    # lock, so the lock is never free while a write is still going on (#57).
    async with lock:
        token = _held.set(_Holding(running, holding.keys | {key}))
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

    Raises ``StageError("integrate", ...)``: this is the integrate stage, so
    this is the edge that names it, whatever host git the strategy ran
    (ADR-0032).
    """
    with attributing("integrate"):
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

    This is how a series that reached no target is not lost — a run that
    failed, conflicted, was cancelled, or integrates nowhere at all. Landing
    at the base can never conflict, and the host's working tree, index and
    HEAD are left untouched. An empty series writes nothing.

    Serialized with landings on the same repo: it writes objects and a ref
    into the host, so it takes the same lock ``integrate`` does (ADR-0033).

    Args:
        repo: The host repo, or an open ``GitRepo`` to reuse.
        branch: The branch to create; a run's own is ``waystation/<run-id>``.
        series: The patches to keep.

    Returns:
        ``branch``, so a caller can report where the work went.

    Raises:
        StageError: Attributed to ``"integrate"`` — keeping a series is a
            landing at the base, and it is attributed like one (ADR-0032).
    """
    if series.patches:
        with attributing("integrate"):
            git_repo = repo if isinstance(repo, GitRepo) else await GitRepo.open(repo)
            async with _serialized(git_repo.common_dir):
                commits = await git_repo.commit_series(series)
                await git_repo.git("update-ref", f"refs/heads/{branch}", commits[-1])
    return branch


@dataclass(frozen=True, slots=True)
class Integration:
    """Shipped strategy: land a series on a named branch, by apply or merge.

    Built from ``GitRepo``'s landing steps alone, as a user's strategy would
    be (ADR-0040). One attempt with the one mechanism: an apply that
    conflicts never falls back to merge (ADR-0015).

    Attributes:
        target: The branch to land on, created at the series' base if missing.
        mechanism: ``"apply"`` replays the series commit by commit, skipping
            any whose change the target already has; ``"merge"`` lands it
            with one two-parent merge commit.
    """

    target: str
    mechanism: Mechanism = "apply"

    async def integrate(self, repo: GitRepo, series: PatchSeries) -> IntegrationReport:
        target = await repo.read_target(self.target, base=series.base_sha)
        if not series.patches:
            return self._report(target, target.tip if target.exists else None)
        commits = await repo.commit_series(series)
        if self.mechanism == "apply":
            landing = await self._apply(repo, target.tip, series.base_sha, commits)
        else:
            landing = await self._merge(repo, target.tip, series.base_sha, commits[-1])
        if isinstance(landing, Conflict):
            # Nothing but unreferenced objects was written (ADR-0020).
            return self._report(target, None, conflict=landing)
        tip, landed = landing
        await repo.move_target(target, tip)
        return self._report(target, tip, landed)

    @staticmethod
    async def _apply(
        repo: GitRepo, tip: str, base: str, commits: tuple[str, ...]
    ) -> tuple[str, tuple[str, ...]] | Conflict:
        """Replay each commit onto ``tip``; the new tip and what landed."""
        landed: list[str] = []
        for index, commit in enumerate(commits):
            parent = commits[index - 1] if index else base
            tree = await repo.merge_tree(base=parent, ours=tip, theirs=commit)
            if isinstance(tree, Conflict):
                subject = await repo.git("log", "-1", "--format=%s", commit)
                return replace(tree, failed_patch=FailedPatch(index, subject))
            if tree == await repo.git("rev-parse", f"{tip}^{{tree}}"):
                continue  # already on the target, as `git am` skips it
            tip = await repo.commit_tree(tree, tip, like=commit)
            landed.append(tip)
        return tip, tuple(landed)

    @staticmethod
    async def _merge(
        repo: GitRepo, tip: str, base: str, series_tip: str
    ) -> tuple[str, tuple[str, ...]] | Conflict:
        """Merge the series onto ``tip`` in one commit; the new tip and it."""
        tree = await repo.merge_tree(base=base, ours=tip, theirs=series_tip)
        if isinstance(tree, Conflict):
            return tree
        if tree == await repo.git("rev-parse", f"{tip}^{{tree}}"):
            return tip, ()
        message = f"Merge {series_tip[:8]} into branch"
        merged = await repo.commit_tree(
            tree, tip, series_tip, like=series_tip, message=message
        )
        return merged, (merged,)

    def _report(
        self,
        target: Target,
        after: str | None,
        landed: tuple[str, ...] = (),
        *,
        conflict: Conflict | None = None,
    ) -> IntegrationReport:
        return IntegrationReport(
            strategy="Integration",
            target=self.target,
            mechanism=self.mechanism,
            target_before=target.tip,
            target_after=after,
            landed=landed,
            conflict=conflict,
        )


@dataclass(frozen=True, slots=True)
class Squash:
    """Shipped strategy: land a run's whole series on a named branch as one commit.

    One ``merge-tree`` of the series' net change against the target, so it
    can land where ``apply`` would conflict partway: a change the series
    made and then undid is never replayed. On a conflict the target stays
    where it was and the run keeps the series, unsquashed, on its
    preservation branch (ADR-0005). Built from ``GitRepo``'s landing steps
    alone, as a user's strategy would be (ADR-0040).

    The commit is authored as the series' last commit was — the agent, who
    is the host identity (ADR-0006) — and committed by the host user at
    landing, as ``apply`` does. It carries no run id: a strategy is handed
    a repo and a series, not a run.

    Attributes:
        target: The branch to land on, created at the series' base if missing.
        message: The squash commit's message. By default a one-commit
            series keeps its own; a longer one takes its first subject as
            the subject, then lists every subject, as a forge's squash-merge
            lists them.
    """

    target: str
    message: str | None = None

    async def integrate(self, repo: GitRepo, series: PatchSeries) -> IntegrationReport:
        target = await repo.read_target(self.target, base=series.base_sha)
        if not series.patches:
            return self._report(target, target.tip if target.exists else None)
        commits = await repo.commit_series(series)
        tree = await repo.merge_tree(
            base=series.base_sha, ours=target.tip, theirs=commits[-1]
        )
        if isinstance(tree, Conflict):
            # Nothing but unreferenced objects was written (ADR-0020).
            return self._report(target, None, conflict=tree)
        tip = target.tip
        landed: tuple[str, ...] = ()
        if tree != await repo.git("rev-parse", f"{target.tip}^{{tree}}"):
            message = self.message
            if message is None and len(commits) > 1:
                message = await self._summary(repo, series.base_sha, commits[-1])
            tip = await repo.commit_tree(
                tree, target.tip, like=commits[-1], message=message
            )
            landed = (tip,)
        await repo.move_target(target, tip)
        return self._report(target, tip, landed)

    @staticmethod
    async def _summary(repo: GitRepo, base: str, series_tip: str) -> str:
        """The first subject, then every subject as a list, oldest first."""
        listed = await repo.git(
            "log", "--reverse", "--format=%s", f"{base}..{series_tip}"
        )
        subjects = listed.splitlines()
        return "\n".join([subjects[0], "", *(f"* {subject}" for subject in subjects)])

    def _report(
        self,
        target: Target,
        after: str | None,
        landed: tuple[str, ...] = (),
        *,
        conflict: Conflict | None = None,
    ) -> IntegrationReport:
        return IntegrationReport(
            strategy="Squash",
            target=self.target,
            mechanism="squash",
            target_before=target.tip,
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


async def _read_head(repo: GitRepo) -> tuple[str | None, str | None]:
    """The branch the checkout has out (``None`` if detached), and its commit.

    The commit is ``None`` on an unborn branch, which a landing creates.
    """
    on = await repo.run("symbolic-ref", "--quiet", "--short", "HEAD")
    tip = await _read_ref(repo, "HEAD")
    return (on.stdout.strip() or None) if on.exit_code == 0 else None, tip


async def _refuse_tracked_changes(repo: GitRepo) -> None:
    """Refuse a checkout with staged or unstaged changes to tracked files."""
    status = await _listing(
        repo, "status", "--porcelain", "--no-renames", "--untracked-files=no"
    )
    if status:
        paths = [entry[3:] for entry in status]  # past the two-letter code
        raise StageError(
            None,
            Refused(
                reason="dirty_tree",
                detail=f"uncommitted changes to tracked files: {_listed(paths)}",
            ),
        )


async def _fast_forward(repo: GitRepo, target: Target, tip: str) -> None:
    """Move the checkout to ``tip`` as ``merge --ff-only`` does, or refuse."""
    moved = Refused(
        reason="target_moved",
        detail=f"{target.branch} moved while the series was landing",
    )
    before = (
        None if target.branch == "HEAD" else target.branch,
        target.tip if target.exists else None,
    )
    # The checks run again at the swap: a run is long, and its user was
    # free to go on working while it ran.
    if await _read_head(repo) != before:
        raise StageError(None, moved)
    await _refuse_tracked_changes(repo)
    in_the_way = await _untracked_in_the_way(repo, target, tip)
    if in_the_way:
        raise StageError(
            None,
            Refused(
                reason="dirty_tree",
                detail=(
                    "untracked files the landing would overwrite: "
                    f"{_listed(in_the_way)}"
                ),
            ),
        )
    if target.exists and tip == target.tip:
        return  # nothing landed: the checkout is already there
    try:
        # --no-autostash: a merge.autoStash in the user's config would put
        # their work aside and back, which is the clobbering this refuses.
        await repo.git("merge", "--ff-only", "--no-autostash", "--quiet", tip)
    except StageError as exc:
        # As with update-ref: a refusal is only ours if the checkout moved.
        if await _read_head(repo) == before:
            raise
        raise StageError(None, moved) from exc


async def _untracked_in_the_way(repo: GitRepo, target: Target, tip: str) -> list[str]:
    """The files not in HEAD's tree that landing ``tip`` would overwrite.

    Only paths the landing adds can hold one: every other path it touches is
    tracked, and the checkout is clean. A file in the way is one on such a
    path, or where a directory above it must go. Ignored files count: git
    would overwrite them without a word, and they are the user's all the same.
    """
    top = Path(await repo.git("rev-parse", "--show-toplevel"))
    if target.exists:
        added = await _listing(
            repo,
            "diff",
            "--name-only",
            "--no-renames",
            "--diff-filter=A",
            target.tip,
            tip,
        )
        tracked = set(
            await _listing(repo, "ls-tree", "-r", "-t", "--name-only", target.tip)
        )
    else:
        # An unborn branch: everything the landing checks out is new.
        added = await _listing(repo, "ls-tree", "-r", "--name-only", tip)
        tracked = set()
    in_the_way: set[str] = set()
    for path in added:
        parts = path.split("/")
        for depth in range(1, len(parts)):
            above = "/".join(parts[:depth])
            on_disk = top / above
            if above not in tracked and (on_disk.is_symlink() or on_disk.is_file()):
                in_the_way.add(above)
        if os.path.lexists(top / path):
            in_the_way.add(path)
    return sorted(in_the_way)


async def _listing(repo: GitRepo, *args: str) -> list[str]:
    """A git listing's entries, ``-z`` separated and never stripped.

    Stripping would eat a path's own leading space; ``-z`` turns off the
    quoting ``core.quotePath`` would otherwise put on a path.
    """
    command, *rest = args
    listed = await repo.run(command, "-z", *rest, check=True)
    return [entry for entry in listed.stdout.split("\0") if entry]


def _listed(paths: list[str]) -> str:
    """Paths for a refusal's detail: a few named, the rest counted."""
    # Five is for a person reading one line, not a bound on any work.
    shown = ", ".join(paths[:5])
    return shown if len(paths) <= 5 else f"{shown} and {len(paths) - 5} more"


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
