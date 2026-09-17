"""Collect: salvage, linearity check, format-patch → PatchSeries."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from waystation.errors import StageError
from waystation.results import CommandFailed, Refused, Series
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
        base_sha = _host_git(host, "rev-parse", "--verify", base).stdout.strip()
        result = _host_git(host, "format-patch", "--stdout", f"{base_sha}..{ref}")
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


def _decode(output: bytes) -> str:
    # Never use text-mode pipes for git: on Windows they rewrite "\n" as "\r\n"
    # (and back), which changes every line of a patch. surrogateescape keeps
    # bytes that are not UTF-8.
    return output.decode("utf-8", errors="surrogateescape")


def _encode(text: str) -> bytes:
    return text.encode("utf-8", errors="surrogateescape")


def _host_git(
    repo: Path,
    *args: str,
    check: bool = True,
    env: dict[str, str] | None = None,
    stage: str = "collect",
) -> subprocess.CompletedProcess[str]:
    argv = ["git", "-C", str(repo), *args]
    raw = subprocess.run(argv, check=False, capture_output=True, env=env)
    result = subprocess.CompletedProcess(
        argv, raw.returncode, _decode(raw.stdout), _decode(raw.stderr)
    )
    if check and result.returncode != 0:
        raise StageError(
            stage,  # type: ignore[arg-type]
            CommandFailed(
                argv=tuple(argv),
                exit_code=result.returncode,
                stderr_tail=bound_tail(result.stderr),
            ),
        )
    return result


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


def preserve_series(
    host_repo: Path | str,
    *,
    branch: str,
    series: PatchSeries,
) -> str:
    """Land ``series`` on ``branch`` at ``series.base_sha`` via plumbing.

    Returns the branch name. Leaves the host working tree, index, and HEAD untouched.
    """
    host = Path(host_repo).resolve()
    if not series.patches:
        return branch

    name_r = _host_git(host, "config", "--get", "user.name", check=False)
    email_r = _host_git(host, "config", "--get", "user.email", check=False)
    if (
        name_r.returncode != 0
        or email_r.returncode != 0
        or not name_r.stdout.strip()
        or not email_r.stdout.strip()
    ):
        raise StageError(
            "collect",
            Refused(
                reason="no_git_identity",
                detail="host repo has no git identity (user.name / user.email)",
            ),
        )
    name = name_r.stdout.strip()
    email = email_r.stdout.strip()

    fd, index_path = tempfile.mkstemp(prefix="waystation-idx-")
    os.close(fd)
    index = Path(index_path)
    try:
        env = {**os.environ, "GIT_INDEX_FILE": str(index)}
        _host_git(host, "read-tree", series.base_sha, env=env)
        parent = series.base_sha

        for patch_text in series.patches:
            parent = _commit_patch(
                host,
                patch_text=patch_text,
                parent=parent,
                committer_name=name,
                committer_email=email,
                env=env,
            )

        _host_git(host, "update-ref", f"refs/heads/{branch}", parent)
    finally:
        index.unlink(missing_ok=True)

    return branch


_SUBJECT_PATCH_PREFIX = re.compile(r"^\[PATCH(?:\s+\d+/\d+)?\]\s*")


def _commit_patch(
    host: Path,
    *,
    patch_text: str,
    parent: str,
    committer_name: str,
    committer_email: str,
    env: dict[str, str],
    stage: str = "collect",
) -> str:
    with tempfile.TemporaryDirectory(prefix="waystation-patch-") as tmp:
        tmp_path = Path(tmp)
        msg_file = tmp_path / "MSG"
        diff_file = tmp_path / "DIFF"
        mail = subprocess.run(
            ["git", "mailinfo", str(msg_file), str(diff_file)],
            input=_encode(patch_text),
            check=False,
            capture_output=True,
            cwd=host,
        )
        if mail.returncode != 0:
            raise StageError(
                stage,  # type: ignore[arg-type]
                CommandFailed(
                    argv=("git", "mailinfo"),
                    exit_code=mail.returncode,
                    stderr_tail=bound_tail(_decode(mail.stderr)),
                ),
            )
        author_name, author_email, subject = _parse_mailinfo(_decode(mail.stdout))
        subject = _SUBJECT_PATCH_PREFIX.sub("", subject).strip() or "commit"
        body = _decode(msg_file.read_bytes())
        message = subject if not body.strip() else f"{subject}\n\n{body}"
        msg_file.write_bytes(_encode(message))

        author_date = _parse_date(patch_text)
        if diff_file.stat().st_size > 0:
            apply = subprocess.run(
                ["git", "-C", str(host), "apply", "--cached", str(diff_file)],
                check=False,
                capture_output=True,
                env=env,
            )
            if apply.returncode != 0:
                raise StageError(
                    stage,  # type: ignore[arg-type]
                    CommandFailed(
                        argv=("git", "apply", "--cached"),
                        exit_code=apply.returncode,
                        stderr_tail=bound_tail(_decode(apply.stderr)),
                    ),
                )

        tree = _host_git(host, "write-tree", env=env, stage=stage).stdout.strip()
        commit_env = {
            **env,
            "GIT_AUTHOR_NAME": author_name or committer_name,
            "GIT_AUTHOR_EMAIL": author_email or committer_email,
            "GIT_COMMITTER_NAME": committer_name,
            "GIT_COMMITTER_EMAIL": committer_email,
        }
        if author_date:
            commit_env["GIT_AUTHOR_DATE"] = author_date
        commit = _host_git(
            host,
            "commit-tree",
            tree,
            "-p",
            parent,
            "-F",
            str(msg_file),
            env=commit_env,
            stage=stage,
        ).stdout.strip()
        return commit


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
