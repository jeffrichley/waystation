"""Landing onto the host's HEAD, through the public run API (issue #25)."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from helpers import a_run, assert_refused, git, host_state, subjects
from waystation import (
    Integration,
    PatchSeries,
    Refused,
    RunConflicted,
    RunFailed,
    RunSucceeded,
    ScriptedCommit,
    Squash,
    integrate,
)
from waystation.integration import (
    GitRepo,
    GitResult,
    IntegrationReport,
    IntegrationStrategy,
)

pytestmark = pytest.mark.git

STRATEGIES = pytest.mark.parametrize(
    "strategy",
    [Integration("HEAD"), Integration("HEAD", mechanism="merge"), Squash("HEAD")],
    ids=["apply", "merge", "squash"],
)


@STRATEGIES
async def test_a_run_lands_on_the_checked_out_branch_and_the_checkout_follows(
    host_repo: Path, strategy: IntegrationStrategy
) -> None:
    home = git(host_repo, "symbolic-ref", "HEAD")
    before = git(host_repo, "rev-parse", "HEAD")

    result = await a_run(host_repo).integrate(strategy)

    assert isinstance(result, RunSucceeded)
    assert result.report is not None
    assert result.report.target == "HEAD"
    assert result.report.target_before == before
    after = result.report.target_after
    assert after is not None and after != before
    assert git(host_repo, "symbolic-ref", "HEAD") == home, "still on its branch"
    assert git(host_repo, "rev-parse", "HEAD") == after
    assert (host_repo / "a.txt").read_bytes() == b"x", "the checkout has it"
    assert git(host_repo, "status", "--porcelain") == ""
    assert result.preserved is None


async def test_a_run_lands_on_a_detached_head(host_repo: Path) -> None:
    home = git(host_repo, "symbolic-ref", "--short", "HEAD")
    git(host_repo, "checkout", "-q", "--detach")
    before = git(host_repo, "rev-parse", "HEAD")

    result = await a_run(host_repo).integrate("HEAD")

    assert isinstance(result, RunSucceeded)
    assert result.report is not None
    assert git(host_repo, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"
    assert git(host_repo, "rev-parse", "HEAD") == result.report.target_after
    assert git(host_repo, "rev-parse", "HEAD^") == before
    assert git(host_repo, "rev-parse", home) == before, "no branch moved"
    assert (host_repo / "a.txt").read_bytes() == b"x"


async def test_a_run_lands_on_an_unborn_branch_and_creates_it(host_repo: Path) -> None:
    base = git(host_repo, "rev-parse", "HEAD")
    git(host_repo, "checkout", "-q", "--orphan", "fresh")
    git(host_repo, "rm", "-rfq", "--cached", ".")
    (host_repo / "README").unlink()

    result = await a_run(host_repo).base(base).integrate("HEAD")

    assert isinstance(result, RunSucceeded)
    assert result.report is not None
    assert git(host_repo, "symbolic-ref", "HEAD") == "refs/heads/fresh"
    assert git(host_repo, "rev-parse", "fresh") == result.report.target_after
    assert git(host_repo, "rev-parse", "fresh^") == base
    assert (host_repo / "a.txt").read_bytes() == b"x"
    assert (host_repo / "README").exists(), "the base came with it"


def staged(repo: Path) -> None:
    (repo / "README").write_bytes(b"staged edit\n")
    git(repo, "add", "README")


def unstaged(repo: Path) -> None:
    (repo / "README").write_bytes(b"unstaged edit\n")


def staged_new_file(repo: Path) -> None:
    (repo / "new.txt").write_bytes(b"added, never committed\n")
    git(repo, "add", "new.txt")


@pytest.mark.parametrize(
    "dirty", [staged, unstaged, staged_new_file], ids=["staged", "unstaged", "added"]
)
@STRATEGIES
async def test_a_dirty_tree_is_refused_before_anything_moves(
    host_repo: Path,
    strategy: IntegrationStrategy,
    dirty: Callable[[Path], None],
) -> None:
    dirty(host_repo)
    before = host_state(host_repo)

    result = await a_run(host_repo).integrate(strategy)

    assert_refused(result, "dirty_tree", host_repo)
    assert host_state(host_repo, ignoring=result.preserved) == before


@pytest.mark.parametrize(
    ("untracked", "landing"),
    [
        ("a.txt", {"a.txt": "x"}),
        ("docs", {"docs/a.txt": "x"}),
        ("deep/a.txt", {"deep/a.txt": "x"}),
    ],
    ids=["same-path", "file-where-a-dir-lands", "in-a-new-dir"],
)
async def test_an_untracked_file_the_landing_would_overwrite_is_refused(
    host_repo: Path, untracked: str, landing: dict[str, str]
) -> None:
    (host_repo / untracked).parent.mkdir(parents=True, exist_ok=True)
    (host_repo / untracked).write_bytes(b"mine, never committed\n")
    before = host_state(host_repo)
    commits = (ScriptedCommit("add a file", landing),)

    result = await a_run(host_repo, commits=commits).integrate("HEAD")

    assert_refused(result, "dirty_tree", host_repo)
    assert isinstance(result, RunFailed) and isinstance(result.failure, Refused)
    assert untracked in result.failure.detail
    assert host_state(host_repo, ignoring=result.preserved) == before


async def test_an_ignored_file_the_landing_would_overwrite_is_refused(
    host_repo: Path,
) -> None:
    # Git itself would overwrite an ignored file without a word; it is still
    # the user's, and only they may throw it away.
    (host_repo / ".git" / "info" / "exclude").write_bytes(b"a.txt\n")
    (host_repo / "a.txt").write_bytes(b"mine, ignored\n")

    result = await a_run(host_repo).integrate("HEAD")

    assert_refused(result, "dirty_tree", host_repo)
    assert (host_repo / "a.txt").read_bytes() == b"mine, ignored\n"


async def test_untracked_files_the_landing_leaves_alone_do_not_block_it(
    host_repo: Path,
) -> None:
    (host_repo / "logs").mkdir()
    (host_repo / "logs" / "run.log").write_bytes(b"a log\n")
    (host_repo / "a.txt.bak").write_bytes(b"near, but not the path\n")

    result = await a_run(host_repo).integrate("HEAD")

    assert isinstance(result, RunSucceeded)
    assert (host_repo / "a.txt").read_bytes() == b"x"
    assert (host_repo / "logs" / "run.log").read_bytes() == b"a log\n"
    assert (host_repo / "a.txt.bak").read_bytes() == b"near, but not the path\n"


CLAIMS_README = (ScriptedCommit("claim readme", {"README": "agent\n"}),)


@STRATEGIES
async def test_a_conflict_onto_head_leaves_the_working_tree_untouched(
    host_repo: Path, strategy: IntegrationStrategy
) -> None:
    base = git(host_repo, "rev-parse", "HEAD")
    (host_repo / "README").write_bytes(b"theirs\n")
    git(host_repo, "commit", "-q", "-am", "theirs")
    (host_repo / "logs").mkdir()
    (host_repo / "logs" / "run.log").write_bytes(b"a log\n")
    before = host_state(host_repo)

    result = (
        await a_run(host_repo, commits=CLAIMS_README).base(base).integrate(strategy)
    )

    assert isinstance(result, RunConflicted)
    assert result.report.target == "HEAD"
    assert result.report.target_after is None
    assert result.report.conflict is not None
    assert result.report.conflict.paths == ("README",)
    assert host_state(host_repo, ignoring=result.preserved) == before
    assert subjects(host_repo, f"{base}..{result.preserved}") == ["claim readme"]


def commits_outside(repo: Path) -> None:
    git(repo, "commit", "-q", "--allow-empty", "-m", "outside")


def switches_branch(repo: Path) -> None:
    git(repo, "checkout", "-q", "-b", "elsewhere")


def edits_readme(repo: Path) -> None:
    (repo / "README").write_bytes(b"edited mid-landing\n")


def drops_a_file_in_the_way(repo: Path) -> None:
    (repo / "a.txt").write_bytes(b"mine, made mid-landing\n")


@dataclass(frozen=True)
class Interrupted(GitRepo):
    """A host whose user does ``does`` to the checkout just before ``at`` runs."""

    at: str = "merge"
    does: Callable[[Path], None] = commits_outside
    done: list[bool] = field(default_factory=list)

    async def run(
        self,
        *args: str,
        check: bool = False,
        stdin: bytes | None = None,
        env: Mapping[str, str] | None = None,
    ) -> GitResult:
        if args[:1] == (self.at,) and not self.done:
            self.does(self.path)
            self.done.append(True)
        return await super().run(*args, check=check, stdin=stdin, env=env)


@dataclass(frozen=True)
class Raced:
    """Wraps a strategy, handing it a host whose user works mid-landing."""

    inner: IntegrationStrategy
    at: str
    does: Callable[[Path], None] = commits_outside

    async def integrate(self, repo: GitRepo, series: PatchSeries) -> IntegrationReport:
        racing = Interrupted(repo.path, repo.common_dir, at=self.at, does=self.does)
        return await self.inner.integrate(racing, series)


@pytest.mark.parametrize(
    "at", ["merge-tree", "merge"], ids=["before-the-swap", "during-the-swap"]
)
@STRATEGIES
async def test_a_checkout_that_moved_is_refused_and_stays_moved(
    host_repo: Path, strategy: IntegrationStrategy, at: str
) -> None:
    result = await a_run(host_repo).integrate(Raced(strategy, at=at))

    assert_refused(result, "target_moved", host_repo)
    assert git(host_repo, "log", "-1", "--format=%s", "HEAD") == "outside"
    assert not (host_repo / "a.txt").exists()


async def test_a_checkout_switched_to_another_branch_is_target_moved(
    host_repo: Path,
) -> None:
    home = git(host_repo, "symbolic-ref", "--short", "HEAD")
    tip = git(host_repo, "rev-parse", home)
    raced = Raced(Integration("HEAD"), at="merge-tree", does=switches_branch)

    result = await a_run(host_repo).integrate(raced)

    assert_refused(result, "target_moved", host_repo)
    assert git(host_repo, "symbolic-ref", "--short", "HEAD") == "elsewhere"
    assert git(host_repo, "rev-parse", "elsewhere") == tip
    assert git(host_repo, "rev-parse", home) == tip


@pytest.mark.parametrize(
    ("does", "kept"),
    [(edits_readme, "README"), (drops_a_file_in_the_way, "a.txt")],
    ids=["tracked-edit", "untracked-in-the-way"],
)
async def test_work_the_user_did_while_it_landed_is_refused_not_clobbered(
    host_repo: Path, does: Callable[[Path], None], kept: str
) -> None:
    raced = Raced(Integration("HEAD"), at="merge-tree", does=does)
    before = git(host_repo, "rev-parse", "HEAD")

    result = await a_run(host_repo).integrate(raced)

    assert_refused(result, "dirty_tree", host_repo)
    assert git(host_repo, "rev-parse", "HEAD") == before
    assert b"mid-landing" in (host_repo / kept).read_bytes()


async def test_nothing_to_land_leaves_head_alone(host_repo: Path) -> None:
    before = host_state(host_repo)

    result = await a_run(host_repo, commits=()).integrate("HEAD")

    assert isinstance(result, RunSucceeded)
    assert result.report is not None
    assert result.report.landed == ()
    assert host_state(host_repo) == before


def commit_of(repo: Path, files: Mapping[str, bytes]) -> str:
    """A commit atop HEAD whose tree is exactly ``files``, the checkout untouched.

    Built in a scratch index, so it can delete, rename or recase a path —
    what a ``ScriptedCommit`` cannot.
    """
    env = {**os.environ, "GIT_INDEX_FILE": str(repo / ".git" / "scratch-index")}
    for path, data in files.items():
        blob = (
            subprocess.run(
                ["git", "hash-object", "-w", "--stdin"],
                cwd=repo,
                input=data,
                capture_output=True,
                check=True,
            )
            .stdout.decode()
            .strip()
        )
        subprocess.run(
            ["git", "update-index", "--add", "--cacheinfo", f"100644,{blob},{path}"],
            cwd=repo,
            env=env,
            check=True,
        )
    tree = (
        subprocess.run(
            ["git", "write-tree"], cwd=repo, env=env, capture_output=True, check=True
        )
        .stdout.decode()
        .strip()
    )
    (repo / ".git" / "scratch-index").unlink()
    return git(repo, "commit-tree", tree, "-p", "HEAD", "-m", "reshape")


async def test_a_host_opened_from_a_subdirectory_lands_every_path(
    host_repo: Path,
) -> None:
    # README becomes a directory: a tracked file replaced, not one in the way.
    (host_repo / "sub").mkdir()
    (host_repo / "sub" / "keep").write_bytes(b"k\n")
    git(host_repo, "add", "sub")
    git(host_repo, "commit", "-q", "-m", "a subdirectory")
    base = git(host_repo, "rev-parse", "HEAD")
    tip = commit_of(host_repo, {"README/x": b"x\n", "sub/keep": b"k\n"})
    series = await PatchSeries.from_range(host_repo, base, tip)
    repo = await GitRepo.open(host_repo / "sub")

    report = await integrate(repo, series, Integration("HEAD"))

    assert report.target_after == git(host_repo, "rev-parse", "HEAD")
    assert (host_repo / "README" / "x").read_bytes() == b"x\n"


async def test_a_landing_that_only_recases_a_path_is_not_in_its_own_way(
    host_repo: Path,
) -> None:
    # On a case-insensitive filesystem the new name is already on disk,
    # and it is the tracked file under its old case.
    base = git(host_repo, "rev-parse", "HEAD")
    tip = commit_of(host_repo, {"readme": b"committed\n"})
    series = await PatchSeries.from_range(host_repo, base, tip)

    report = await integrate(host_repo, series, Integration("HEAD"))

    assert report.target_after == git(host_repo, "rev-parse", "HEAD")
    assert git(host_repo, "ls-files") == "readme"


FLOW = """\
import asyncio
import sys
from pathlib import Path

from pydantic import BaseModel

from waystation import Flow, NoSandbox, RunLogFiles, ScriptedAgent, ScriptedCommit

here = Path(__file__).resolve().parent


class Summary(BaseModel):
    summary: str


async def main() -> int:
    flow = Flow(
        here,
        agent=ScriptedAgent(
            commits=(ScriptedCommit("improve the flow", {"notes.txt": "n\\n"}),),
            outcome=Summary(summary="ok"),
        ),
        sandbox=NoSandbox(),
        hooks=[RunLogFiles(here / "logs")],
    )
    result = await flow.run("improve yourself", outcome=Summary).integrate("HEAD")
    print(type(result).__name__)
    return 0 if type(result).__name__ == "RunSucceeded" else 1


sys.exit(asyncio.run(main()))
"""


async def test_a_flow_lands_onto_the_head_of_the_repo_that_holds_it(
    host_repo: Path, isolated_tempdir: Path
) -> None:
    (host_repo / "flow.py").write_bytes(textwrap.dedent(FLOW).encode())
    git(host_repo, "add", "flow.py")
    git(host_repo, "commit", "-q", "-m", "add the flow")
    before = git(host_repo, "rev-parse", "HEAD")
    temp = str(isolated_tempdir)

    ran = subprocess.run(
        [sys.executable, "flow.py"],
        cwd=host_repo,
        capture_output=True,
        env={**os.environ, "TMPDIR": temp, "TEMP": temp, "TMP": temp},
        check=False,
    )

    assert ran.returncode == 0, ran.stdout + ran.stderr
    assert git(host_repo, "rev-parse", "HEAD^") == before
    assert (host_repo / "notes.txt").read_bytes() == b"n\n"
    assert list((host_repo / "logs").glob("*.log")), "its own logs didn't block it"
    assert git(host_repo, "status", "--porcelain", "--untracked-files=no") == ""
