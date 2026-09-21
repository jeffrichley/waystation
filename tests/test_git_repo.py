"""`GitRepo` is the one way in to host git, and landings serialize (#71, ADR-0033).

The rules here are the ones a *user's* strategy depends on: it can do
everything the shipped one does, and a landing it starts is serialized per
repo however many event loops the host process runs.
"""

from __future__ import annotations

import ast
import asyncio
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from helpers import commit_on, git
from waystation import (
    GitRepo,
    GitResult,
    Integration,
    IntegrationReport,
    IntegrationStrategy,
    PatchSeries,
    integrate,
    preserve_series,
)
from waystation.integration import Conflict

TARGET = "agents/landed"


async def _a_series(repo: Path, name: str = "work") -> PatchSeries:
    """One commit on its own branch, as a series to land."""
    base = git(repo, "rev-parse", "HEAD")
    commit_on(repo, name, {f"{name}.txt": f"{name}\n"}, message=f"the {name}")
    return await PatchSeries.from_range(repo, base, name)


async def _settle() -> None:
    """Let every runnable task reach its next await; no wall time passes.

    A task parked on the repo's lock stays parked however many turns pass,
    so a marker its strategy would have written at once stays absent. Five
    turns is generous: reaching the lock takes one.
    """
    for _ in range(5):
        await asyncio.sleep(0)


@dataclass(frozen=True)
class Marks:
    """Writes ``marker`` the moment it is let into the repo, then lands."""

    marker: Path

    async def integrate(self, repo: GitRepo, series: PatchSeries) -> IntegrationReport:
        self.marker.write_text("in", encoding="utf-8")
        return await Integration(TARGET).integrate(repo, series)


@pytest.mark.git
def test_a_second_event_loop_can_still_contend_for_the_same_repo(
    host_repo: Path,
) -> None:
    """An ``asyncio.Lock`` binds to the loop that first *contends* for it.

    A process that runs more than one loop — a service calling
    ``asyncio.run`` per request, a suite with a loop per test — failed its
    next contended landing with ``RuntimeError``, reported as
    ``RunFailed(integrate, Errored)``.
    """

    async def two_at_once(name: str) -> None:
        repo = await GitRepo.open(host_repo)
        series = await _a_series(host_repo, name)
        held = asyncio.Event()
        let_go = asyncio.Event()

        @dataclass(frozen=True)
        class Holds:
            async def integrate(
                self, repo: GitRepo, series: PatchSeries
            ) -> IntegrationReport:
                held.set()
                await let_go.wait()
                return await Integration(TARGET).integrate(repo, series)

        first = asyncio.create_task(integrate(repo, series, Holds()))
        await held.wait()
        # It reaches the lock and waits there: that is what binds the lock.
        second = asyncio.create_task(integrate(repo, series, Integration(TARGET)))
        await _settle()
        assert not second.done()
        let_go.set()
        await asyncio.gather(first, second)

    asyncio.run(two_at_once("first"))
    asyncio.run(two_at_once("second"))

    assert "second" in git(host_repo, "show", f"{TARGET}:second.txt")


@dataclass(frozen=True)
class HoldsAtUpdateRef(GitRepo):
    """A host that waits inside ``update-ref`` until the test lets it go."""

    reached: asyncio.Event = field(default_factory=asyncio.Event)
    let_go: asyncio.Event = field(default_factory=asyncio.Event)

    async def git(self, *args: str, env: Mapping[str, str] | None = None) -> str:
        if args[:1] == ("update-ref",) and not self.let_go.is_set():
            self.reached.set()
            await self.let_go.wait()
        return await super().git(*args, env=env)


@pytest.mark.git
async def test_preservation_waits_for_a_landing_on_the_same_repo(
    host_repo: Path, tmp_path: Path
) -> None:
    """Preservation writes objects and a ref, so it takes the same lock.

    It used to write them outside it, which is the suspected cause of a
    one-off Windows CI failure where one of three concurrent runs failed
    while another was cloning the object store.
    """
    opened = await GitRepo.open(host_repo)
    repo = HoldsAtUpdateRef(opened.path, opened.common_dir)
    series = await _a_series(host_repo)
    marker = tmp_path / "marker"

    keeping = asyncio.create_task(
        preserve_series(repo, branch="waystation/kept", series=series)
    )
    await repo.reached.wait()  # preservation is mid-write, inside the lock

    landing = asyncio.create_task(integrate(repo, series, Marks(marker)))
    await _settle()
    assert not marker.exists(), "the landing got in while preservation was writing"

    repo.let_go.set()
    await asyncio.gather(keeping, landing)
    assert marker.exists()
    assert git(host_repo, "rev-parse", "--verify", "waystation/kept")
    assert "work" in git(host_repo, "show", f"{TARGET}:work.txt")


@dataclass(frozen=True)
class PreservesWhatItCannotLand:
    """A strategy that keeps the series instead of landing it.

    Serialization is at the entry points, so this calls one from inside
    another: reentrant, or it deadlocks with no error at all.
    """

    branch: str

    async def integrate(self, repo: GitRepo, series: PatchSeries) -> IntegrationReport:
        await preserve_series(repo, branch=self.branch, series=series)
        return IntegrationReport(
            strategy="PreservesWhatItCannotLand",
            target=self.branch,
            mechanism="apply",
            target_before=series.base_sha,
            target_after=None,
            landed=(),
        )


@pytest.mark.git
async def test_a_strategy_that_preserves_inside_its_own_integrate_lands_no_deadlock(
    host_repo: Path,
) -> None:
    series = await _a_series(host_repo)

    # A guard, not a bound: a deadlock here has no error to fail on, and a
    # hung test would otherwise run until CI gave up on the whole job.
    async with asyncio.timeout(30):
        report = await integrate(
            host_repo, series, PreservesWhatItCannotLand("waystation/kept")
        )

    assert report.target_after is None
    assert "work" in git(host_repo, "show", "waystation/kept:work.txt")


@dataclass(frozen=True)
class SpawnsALanding:
    """Starts a second landing as a child task, while it holds the repo.

    The child inherits this task's context, so reentrancy read from the
    context alone would wave it past the lock — two landings at once on one
    repo, the thing serialization exists to prevent.
    """

    inner: IntegrationStrategy
    marker: Path
    started: list[asyncio.Task[IntegrationReport]] = field(default_factory=list)
    got_in: list[bool] = field(default_factory=list)

    async def integrate(self, repo: GitRepo, series: PatchSeries) -> IntegrationReport:
        self.started.append(asyncio.create_task(integrate(repo, series, self.inner)))
        await _settle()
        self.got_in.append(self.marker.exists())
        return IntegrationReport(
            strategy="SpawnsALanding",
            target="none",
            mechanism="apply",
            target_before=series.base_sha,
            target_after=None,
            landed=(),
        )


@pytest.mark.git
async def test_a_landing_spawned_as_a_child_task_still_waits_its_turn(
    host_repo: Path, tmp_path: Path
) -> None:
    """Reentrancy is the same task's, never a task it started."""
    series = await _a_series(host_repo)
    marker = tmp_path / "marker"
    spawner = SpawnsALanding(Marks(marker), marker)

    await integrate(host_repo, series, spawner)

    assert spawner.got_in == [False], "a child task walked past the lock"
    (child,) = spawner.started
    await child  # the outer landing is done, so its turn has come
    assert marker.exists()
    assert "work" in git(host_repo, "show", f"{TARGET}:work.txt")


@dataclass(frozen=True)
class ByHand:
    """Lands a series through nothing but the public ``GitRepo``.

    It does the three things the shipped strategy needed private git for:
    reads an exit code, feeds a patch in on stdin as bytes, and moves a ref.
    """

    target: str
    exit_codes: list[int] = field(default_factory=list)

    async def integrate(self, repo: GitRepo, series: PatchSeries) -> IntegrationReport:
        ref = f"refs/heads/{self.target}"
        shown = await repo.run("rev-parse", "--verify", "--quiet", ref)
        self.exit_codes.append(shown.exit_code)
        before = shown.stdout.strip() if shown.exit_code == 0 else series.base_sha

        tip, landed = before, []
        with tempfile.TemporaryDirectory(prefix="by-hand-") as tmp:
            msg, diff, index = (Path(tmp) / part for part in ("MSG", "DIFF", "INDEX"))
            env = {**os.environ, "GIT_INDEX_FILE": str(index)}
            for patch in series.patches:
                mail = await repo.run(
                    "mailinfo",
                    str(msg),
                    str(diff),
                    stdin=patch.encode("utf-8", errors="surrogateescape"),
                    check=True,
                )
                # mailinfo splits a patch: headers on stdout, body and diff
                # into the two files it was given.
                subject = next(
                    line.removeprefix("Subject: ")
                    for line in mail.stdout.splitlines()
                    if line.startswith("Subject: ")
                )
                msg.write_text(subject, encoding="utf-8")
                await repo.git("read-tree", tip, env=env)
                if diff.stat().st_size:
                    await repo.git("apply", "--cached", str(diff), env=env)
                tree = await repo.git("write-tree", env=env)
                tip = await repo.git("commit-tree", tree, "-p", tip, "-F", str(msg))
                landed.append(tip)
        await repo.git("update-ref", ref, tip)
        return IntegrationReport(
            strategy="ByHand",
            target=self.target,
            mechanism="apply",
            target_before=before,
            target_after=tip,
            landed=tuple(landed),
        )


@pytest.mark.git
async def test_a_strategy_built_from_the_public_surface_lands_a_series(
    host_repo: Path,
) -> None:
    """#18 story 113: a git runner, so custom landing rules stay small."""
    series = await _a_series(host_repo)
    by_hand = ByHand(TARGET)

    report = await integrate(host_repo, series, by_hand)

    assert by_hand.exit_codes == [1], "a missing ref is an exit code, not a failure"
    assert report.target_after is not None
    assert len(report.landed) == 1
    assert "work" in git(host_repo, "show", f"{TARGET}:work.txt")
    assert git(host_repo, "log", "-1", "--format=%s", TARGET) == "the work"


@pytest.mark.git
async def test_a_run_hands_back_the_exit_code_and_both_streams(
    host_repo: Path,
) -> None:
    repo = await GitRepo.open(host_repo)

    failed = await repo.run("rev-parse", "--verify", "refs/heads/nope")

    assert isinstance(failed, GitResult)
    assert failed.exit_code != 0
    assert failed.stdout == ""
    assert failed.stderr.startswith("fatal: ")


@pytest.mark.git
async def test_output_reaches_a_strategy_as_git_wrote_it(host_repo: Path) -> None:
    """Never newline-translated: a patch through a text pipe comes out changed."""
    repo = await GitRepo.open(host_repo)

    listed = await repo.run("rev-list", "--max-count=1", "HEAD")

    assert listed.stdout.endswith("\n")
    assert "\r" not in listed.stdout


@pytest.mark.unit
def test_the_shipped_strategy_takes_no_private_path_to_host_git() -> None:
    """Whatever ``Integration`` needs, a user's strategy can have (ADR-0033).

    The shipped strategy used to go round ``GitRepo`` into the private runner
    three times — for an exit code, for unstripped stdout, for stdin — so a
    user's strategy could not do what it does. Only ``_repo_git``, which is
    what ``GitRepo`` runs, reaches the private runner now.

    (``GitRepo``'s landing steps still go through ``_git.git_identity``:
    ADR-0033 puts the identity check there, in one place, for the workspace
    path too.)
    """
    from waystation import integration

    tree = ast.parse(Path(integration.__file__).read_text(encoding="utf-8"))
    runs_git = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
        for call in ast.walk(node)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id == "run_git"
    }

    assert runs_git == {"_repo_git"}, runs_git


@pytest.mark.unit
def test_the_shipped_strategies_name_nothing_private_to_land_a_series() -> None:
    """``Integration`` and ``Squash`` are policy over public steps (ADR-0040).

    Every name either strategy's body reaches for is public: ``GitRepo``'s
    landing steps and the result values. A private helper of the module is
    a path a user's strategy could not take — the thing #81 removed.
    """
    from waystation import integration

    tree = ast.parse(Path(integration.__file__).read_text(encoding="utf-8"))
    # The module's own private names, and what it imports from private modules.
    private = {
        name
        for name, value in vars(integration).items()
        if (name.startswith("_") and not name.startswith("__"))
        or str(getattr(value, "__module__", "")).startswith("waystation._")
    }
    strategies = [
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name in ("Integration", "Squash")
    ]
    reached = {
        name.id
        for strategy in strategies
        for name in ast.walk(strategy)
        if isinstance(name, ast.Name)
    }

    assert len(strategies) == 2
    assert {"_read_ref", "run_git"} <= private, "the scan sees what it guards"
    assert reached & private == set(), reached & private


@dataclass(frozen=True)
class FastForwardOnly:
    """A user's rule: land the series as it is, or not at all.

    Policy over the public landing steps: it lands only when the target has
    not moved past the series' base, so what arrives is exactly what the
    agent committed. Nothing in it is plumbing.
    """

    target: str

    async def integrate(self, repo: GitRepo, series: PatchSeries) -> IntegrationReport:
        target = await repo.read_target(self.target, base=series.base_sha)
        if target.tip != series.base_sha:
            conflict = Conflict(paths=())
            return self._report(target.tip, None, (), conflict)
        commits = await repo.commit_series(series)
        tip = commits[-1] if commits else target.tip
        await repo.move_target(target, tip)
        return self._report(target.tip, tip, commits, None)

    def _report(
        self,
        before: str,
        after: str | None,
        landed: tuple[str, ...],
        conflict: Conflict | None,
    ) -> IntegrationReport:
        return IntegrationReport(
            strategy="FastForwardOnly",
            target=self.target,
            mechanism="fast-forward",
            target_before=before,
            target_after=after,
            landed=landed,
            conflict=conflict,
        )


@pytest.mark.git
async def test_a_strategy_of_a_users_own_lands_through_the_public_steps(
    host_repo: Path,
) -> None:
    """#18 story 113, with the plumbing in reach: a rule is its policy alone."""
    base = git(host_repo, "rev-parse", "HEAD")
    commit_on(host_repo, "work", {"one.txt": "1\n"}, message="one")
    commit_on(host_repo, "work", {"two.txt": "2\n"}, message="two")
    series = await PatchSeries.from_range(host_repo, base, "work")

    report = await integrate(host_repo, series, FastForwardOnly(TARGET))

    assert len(report.landed) == 2
    assert report.target_after == git(host_repo, "rev-parse", TARGET)
    assert git(host_repo, "log", "--format=%s", f"{base}..{TARGET}") == "two\none"
    commit_on(host_repo, TARGET, {"theirs.txt": "t\n"})
    refused = await integrate(host_repo, series, FastForwardOnly(TARGET))
    assert refused.conflict is not None, "a moved target is its rule's to refuse"
