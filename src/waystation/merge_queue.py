"""Merge queue: land each candidate only once its check passes on the target's head.

``integrate`` serializes the write to a target and never re-checks it, so two
series each green against one base can land one after the other and leave the
target red. A merge queue closes that gap the way every gating system since the
Not Rocket Science Rule has: test each candidate applied to the target's tip
exactly as it will land, and land exactly what was tested (ADR-0049).

One target per queue, one candidate at a time, in arrival order. Git only: a
candidate is a range on the host, the check runs in a sandbox, and pushing the
landed branch anywhere is the caller's (ADR-0004).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path
from typing import Any

from waystation._serving import Queued, Serving
from waystation.collect import PatchSeries
from waystation.errors import StageError
from waystation.flow import RunSpec
from waystation.integration import GitRepo, Integration, Target, integrate
from waystation.ordering import ArrivalOrder, OrderingStrategy
from waystation.results import (
    Conflict,
    Errored,
    Failure,
    IntegrationReport,
    RunResult,
    RunSucceeded,
    Stage,
)
from waystation.sandbox.protocol import SandboxBackend
from waystation.stages import stages
from waystation.workspace import prepare_workspace

__all__ = [
    "Attempt",
    "Candidate",
    "CheckFailed",
    "CheckResult",
    "Landed",
    "Landing",
    "LandingConflicted",
    "LandingFailed",
    "MergeQueue",
    "QueuedCandidate",
    "Resolve",
    "merge_queue",
    "run_check",
]


@dataclass(frozen=True, slots=True)
class Candidate:
    """A series submitted to land: the commits ``base..ref`` holds on the host.

    For a run that landed nowhere, ``ref`` is its preservation branch and
    ``base`` its ``base_sha``; for a branch a person pushed, its fork point.
    The series stays on ``ref`` whatever happens to it here, so a merge queue
    never has a series of its own to keep (ADR-0015).

    Attributes:
        ref: The branch, or any revision, the series ends at.
        base: The revision the series starts from; ``ref`` descends from it.
        name: What results call it, or ``None``.
    """

    ref: str
    base: str
    name: str | None = None


@dataclass(frozen=True, slots=True)
class CheckResult:
    """How a check ended against one commit: its exit code and what it wrote.

    Attributes:
        revision: The commit the check ran against, as a sha.
        exit_code: The check's exit code; zero passed.
        stdout: Everything it wrote to stdout, every byte kept (ADR-0030).
        stderr: The same, for stderr.
    """

    revision: str
    exit_code: int
    stdout: str
    stderr: str

    @property
    def passed(self) -> bool:
        """Whether the check exited zero."""
        return self.exit_code == 0


@dataclass(frozen=True, slots=True)
class Attempt:
    """What stopped a candidate at the front, handed to the caller's resolver.

    Exactly one of ``conflict`` and ``check`` is set. While the candidate is
    at the front nothing else lands on the target, so a resolver run started
    at ``start`` works against the head the candidate will land on.

    Attributes:
        candidate: The candidate as it was submitted.
        ref: The series being landed this round: the candidate's own, or the
            branch the last resolver run kept.
        head: The target's tip the series was re-applied onto.
        start: Where a resolver run starts: ``head`` after a conflict, the
            commit that failed its check after a failed check. That commit
            is on no branch: keep it with a ref of your own if you want it
            after the queue has moved on.
        conflict: The paths re-applying onto ``head`` conflicted on.
        check: The check that failed.
        resolutions: The resolver runs so far, oldest first.
    """

    candidate: Candidate
    ref: str
    head: str
    start: str
    conflict: Conflict | None = None
    check: CheckResult | None = None
    resolutions: tuple[RunResult[Any], ...] = ()

    def __post_init__(self) -> None:
        if (self.conflict is None) == (self.check is None):
            msg = "an attempt holds a conflict or a failed check, never both or neither"
            raise ValueError(msg)

    @property
    def rounds(self) -> int:
        """How many resolver runs this candidate has had."""
        return len(self.resolutions)


type Resolve = Callable[[Attempt], RunSpec[Any] | None]
"""The caller's resolver: a run to perform for an attempt, or ``None`` to give up."""


@dataclass(frozen=True, slots=True)
class Landed:
    """The candidate landed: the target now stands at the commit checked.

    ``check`` is ``None`` only when the range was empty: nothing landed, so
    there was nothing to check.
    """

    candidate: Candidate
    ref: str
    report: IntegrationReport
    check: CheckResult | None
    resolutions: tuple[RunResult[Any], ...] = ()


@dataclass(frozen=True, slots=True)
class LandingConflicted:
    """The series conflicted re-applied onto ``head``; the target never moved."""

    candidate: Candidate
    ref: str
    head: str
    conflict: Conflict
    resolutions: tuple[RunResult[Any], ...] = ()


@dataclass(frozen=True, slots=True)
class CheckFailed:
    """The series re-applied cleanly, and the check failed on the result.

    The target never moved; ``check.revision`` is the commit that failed,
    which no branch holds: the series is kept on ``ref``, and the failing
    combination is ``ref`` re-applied onto the head, which can be rebuilt.
    """

    candidate: Candidate
    ref: str
    check: CheckResult
    resolutions: tuple[RunResult[Any], ...] = ()


@dataclass(frozen=True, slots=True)
class LandingFailed:
    """Something else stopped the candidate: a refusal, git, the sandbox, a resolver.

    ``stage`` is the stage the failure is attributed to, or ``None`` when it
    was none of a run's — the caller's resolver raised, say.
    """

    candidate: Candidate
    ref: str
    stage: Stage | None
    failure: Failure
    resolutions: tuple[RunResult[Any], ...] = ()


type Landing = Landed | LandingConflicted | CheckFailed | LandingFailed
"""What a merge queue yields; ``match`` on the four kinds, as with ``RunResult``."""


async def run_check(
    repo: Path | str,
    revision: str,
    command: str | Sequence[str],
    sandbox: SandboxBackend,
    *,
    env: Mapping[str, str] | None = None,
) -> CheckResult:
    """Run ``command`` in a sandbox against ``revision``'s tree.

    A workspace is cloned at ``revision``, as a run's is, and removed after;
    the host is never touched. This is the check a merge queue runs on each
    candidate, public so a caller can run the same one against the bare
    target — to tell "this candidate broke it" from "it was already red".

    Unbounded, as nothing asked for a limit (ADR-0017); cancelling it kills
    the check's process tree (ADR-0023) and still removes the workspace.

    Args:
        repo: The host repo.
        revision: The commit to check; resolved to a sha.
        command: A command string, run under the sandbox's own shell
            (ADR-0036), or an argv run directly.
        sandbox: Where the check runs.
        env: Literal values laid over the sandbox's environment.

    Returns:
        The check's exit code and everything it wrote; a failing check is a
        result, not a raise.

    Raises:
        StageError: At ``"workspace"`` or ``"sandbox"``, when either could not
            be made — a primitive raises (ADR-0016).
    """
    async with stages() as run:
        ws = await run.stage("workspace", prepare_workspace(repo, base=revision))
        try:
            async with run.entering(
                "sandbox", sandbox.start(ws, env=dict(env or {}))
            ) as box:
                argv = (
                    [*box.shell, command] if isinstance(command, str) else list(command)
                )
                done = await box.exec(argv, capture=True)
        finally:
            await run.anyway("workspace", ws.remove(), bound=None)
    return CheckResult(
        revision=ws.base_sha,
        exit_code=done.exit_code,
        stdout=done.stdout,
        stderr=done.stderr,
    )


@dataclass(frozen=True, slots=True)
class _Rehearsing(GitRepo):
    """The host repo, but a target is only noted where a landing would move it.

    ``Integration`` rehearses on this exactly as it would land, and the
    commit it would move the target to is what gets checked (ADR-0049).
    """

    moves: list[tuple[Target, str]] = field(default_factory=list)

    async def move_target(self, target: Target, tip: str) -> None:
        self.moves.append((target, tip))


@dataclass(frozen=True, slots=True)
class _Move:
    """The landing a rehearsal settled on, as a strategy that only moves.

    Through ``integrate``, so the swap holds the repo's lock like any landing;
    ``move_target`` compares and swaps, so a target someone else moved since
    the rehearsal is refused, never overwritten (ADR-0033).
    """

    target: Target
    tip: str
    report: IntegrationReport

    async def integrate(self, repo: GitRepo, series: PatchSeries) -> IntegrationReport:
        await repo.move_target(self.target, self.tip)
        return self.report


@dataclass(frozen=True, slots=True)
class _Front:
    """One candidate at the front of the queue, rounds and all."""

    repo: Path | str
    target: str
    command: str | Sequence[str]
    sandbox: SandboxBackend
    env: Mapping[str, str] | None
    resolve: Resolve | None

    async def land(self, candidate: Candidate) -> Landing:
        """Re-apply, check, land; resolve at the front while the caller says to.

        Never raises but a cancellation: every failure is a value (ADR-0016).
        """
        ref, base = candidate.ref, candidate.base
        resolutions: list[RunResult[Any]] = []
        setback: Attempt | None = None
        try:
            while True:
                if setback is None:
                    settled = await self._round(candidate, ref, base, resolutions)
                    if not isinstance(settled, Attempt):
                        return settled
                    setback = settled
                spec = self.resolve(setback) if self.resolve is not None else None
                if spec is None:
                    return _evicted(setback)
                # Started where the attempt says, and landing nowhere: its
                # series is kept on its own branch, which is the next round.
                result = await spec.base(setback.start).integrate(None)
                resolutions.append(result)
                if isinstance(result, RunSucceeded) and result.preserved is not None:
                    ref, base = result.preserved, setback.head
                    setback = None
                else:
                    # Nothing new to land — the run failed, or kept nothing —
                    # so the tree is the one already tried: the setback stands,
                    # and is not paid for again.
                    setback = replace(setback, resolutions=tuple(resolutions))
        except StageError as err:
            return LandingFailed(
                candidate, ref, err.stage, err.failure, tuple(resolutions)
            )
        except Exception as exc:
            return LandingFailed(candidate, ref, None, Errored(exc), tuple(resolutions))

    async def _round(
        self,
        candidate: Candidate,
        ref: str,
        base: str,
        resolutions: list[RunResult[Any]],
    ) -> Landing | Attempt:
        """One re-apply and check: landed, or the attempt that stopped it."""
        repo = await GitRepo.open(self.repo)
        series = await PatchSeries.from_range(repo.path, base, ref)
        rehearsal = _Rehearsing(path=repo.path, common_dir=repo.common_dir)
        report = await integrate(rehearsal, series, Integration(self.target))
        so_far = tuple(resolutions)
        if report.conflict is not None:
            head = report.target_before
            return Attempt(candidate, ref, head, head, report.conflict, None, so_far)
        if not rehearsal.moves:
            return Landed(candidate, ref, report, None, so_far)  # an empty range
        ((target, tip),) = rehearsal.moves
        checked = await run_check(
            repo.path, tip, self.command, self.sandbox, env=self.env
        )
        if not checked.passed:
            return Attempt(candidate, ref, target.tip, tip, None, checked, so_far)
        landed = await integrate(repo, series, _Move(target, tip, report))
        return Landed(candidate, ref, landed, checked, so_far)


def _evicted(attempt: Attempt) -> Landing:
    """The result for a candidate nobody is resolving any more."""
    if attempt.conflict is not None:
        return LandingConflicted(
            attempt.candidate,
            attempt.ref,
            attempt.head,
            attempt.conflict,
            attempt.resolutions,
        )
    assert attempt.check is not None  # an attempt holds one or the other
    return CheckFailed(
        attempt.candidate, attempt.ref, attempt.check, attempt.resolutions
    )


class _QueuedCandidate(Queued[Landing]):
    """One submitted candidate, and the way to stop it alone.

    ``cancel`` at the front stops its check or resolver run, which cleans up
    (ADR-0017), and the target does not move — unless the swap had already
    begun, which finishes (ADR-0027). Still queued, it never starts. Either
    way it reports nothing, and the next candidate goes on.
    """

    def __init__(self, candidate: Candidate) -> None:
        super().__init__()
        self.candidate = candidate


def _refused(candidate: Candidate, failure: Failure) -> Landing:
    """A candidate refused before it reached the front: its ordering failed."""
    return LandingFailed(candidate, candidate.ref, None, failure)


class _MergeQueue(Serving[_QueuedCandidate, Landing]):
    """The candidates submitted to one target, yielded as each is settled.

    A queue with room for one: the pull, the drain on close and the stop
    that waits are ``Serving``'s, which ``queue()`` shares (ADR-0047,
    ADR-0048).
    """

    def __init__(
        self, front: _Front, order: OrderingStrategy[_QueuedCandidate]
    ) -> None:
        # Room for one: depth 1, no speculation. A candidate is checked
        # against a head nothing else is landing on, which is the whole point.
        super().__init__(
            1, order, closed_message="this merge queue is closed: it takes no more"
        )
        self._front = front

    def submit(
        self, ref: str, *, base: str, name: str | None = None
    ) -> _QueuedCandidate:
        """Submit ``base..ref`` to land; it reaches the front when the queue pulls it.

        With one candidate waiting that is as soon as the front is free;
        with more, the queue's ordering strategy says which goes next
        (ADR-0048). Nothing is read now: the range is read at the front, so
        a branch pushed to meanwhile lands as it is then.

        Args:
            ref: The branch, or revision, the series ends at.
            base: The revision it starts from, which ``ref`` descends from.
            name: What its result calls it.

        Returns:
            The submitted candidate, to stop it alone by.

        Raises:
            RuntimeError: When the queue is closed, or its block was left.
        """
        candidate = Candidate(ref=ref, base=base, name=name)
        return self.start(
            _QueuedCandidate(candidate),
            partial(self._front.land, candidate),
            partial(_refused, candidate),
        )


type MergeQueue = _MergeQueue
"""What ``merge_queue()`` returns, for annotating a helper that takes one."""

type QueuedCandidate = _QueuedCandidate
"""What ``submit()`` returns, for annotating what holds one."""


def merge_queue(
    repo: Path | str,
    target: str,
    *,
    check: str | Sequence[str],
    sandbox: SandboxBackend,
    env: Mapping[str, str] | None = None,
    resolve: Resolve | None = None,
    order: OrderingStrategy[QueuedCandidate] | None = None,
) -> MergeQueue:
    """A merge queue for ``target``: submit candidates; yield each as it is settled.

    Each candidate, when it reaches the front, is re-applied onto the
    target's head as ``Integration(target)`` would land it, without moving
    anything; ``check`` runs in ``sandbox`` against exactly that commit; and
    only a check that passed moves the target, to that commit. One at a
    time, in the order submitted unless ``order`` says otherwise. Every
    candidate reports — ``Landed``, ``LandingConflicted``, ``CheckFailed`` or
    ``LandingFailed`` — and nothing raises mid-iteration (ADR-0007)::

        async with merge_queue(repo, "effort/x", check="just check",
                               sandbox=DockerSandbox("ci")) as landings:
            landings.submit(result.preserved, base=result.base_sha)
            async for landing in landings:
                match landing:
                    case Landed(): push(landing.report.target_after)
                    case _: hold(landing)

    With ``resolve``, a candidate that conflicts or fails its check is not
    evicted straight away: the queue hands the caller an ``Attempt`` and
    performs the run it returns, at the front, where nothing else lands.
    The run starts at ``attempt.start`` and lands nowhere; the branch it
    keeps is the next round. Return ``None`` to give up, and the candidate
    is evicted with every round on its result. How many rounds, and what the
    run's prompt says, are the caller's: the queue counts, never stops
    (ADR-0015, ADR-0017).

    Leaving the block closes the queue, stops the candidate at the front —
    its check or resolver run cleans up, the target unmoved — and never
    starts the rest (ADR-0017).

    Args:
        repo: The host repo the target lives in.
        target: The branch to land on, created at the first candidate's base
            if missing, or ``"HEAD"`` for the checkout (ADR-0041).
        check: The command that decides — a string for the sandbox's shell,
            or an argv. Zero passes.
        sandbox: Where each check runs.
        env: Literal values laid over the sandbox's environment for a check.
        resolve: Given an attempt, the resolver run to perform, or ``None``
            to evict; without it, every setback evicts.
        order: Which waiting candidate reaches the front next, when more
            than one is waiting: asked at each such pull, with every waiting
            ``QueuedCandidate`` oldest first. ``None``, the default, is
            arrival order. A strategy that fails refuses the candidates it
            was ordering, each a ``LandingFailed`` whose ``stage`` is
            ``None`` (ADR-0048).

    Returns:
        An async iterator of landings, and an async context manager that
        yields it.
    """
    return _MergeQueue(
        _Front(repo, target, check, sandbox, env, resolve),
        order if order is not None else ArrivalOrder(),
    )
