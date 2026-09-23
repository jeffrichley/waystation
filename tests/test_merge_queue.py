"""A merge queue: land a candidate only once its check passes on the head (#138).

``integrate`` serializes the write to a target; it never re-checks it. A merge
queue does: one candidate at a time per target, in the order they arrive, each
re-applied onto the target's head as it is when that candidate reaches the
front, checked in a sandbox against exactly that tree, and landed by
fast-forwarding the target to the commit checked. A candidate that conflicts or
fails its check can be handed to a resolver run the caller builds, at the
front, where the head cannot move under it; otherwise it is evicted with a
typed result, and nothing raises (ADR-0049).
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from helpers import (
    OK_OUTCOME_LINE,
    Gate,
    GatedSandbox,
    ShellAgent,
    commit_on,
    git,
    subjects,
    until_batch,
    workspaces,
)
from waystation import (
    Attempt,
    CheckFailed,
    Errored,
    Flow,
    Landed,
    Landing,
    LandingConflicted,
    LandingFailed,
    NoSandbox,
    Refused,
    RunSpec,
    RunSucceeded,
    merge_queue,
    run_check,
)
from waystation.integration import Conflict
from waystation.merge_queue import Candidate, CheckResult, MergeQueue, QueuedCandidate

TARGET = "effort"

# Fails once A and B are both in the tree: each candidate alone is green, the
# two together are not — the gap a write lock alone leaves open.
NOT_BOTH = "test ! -e A || test ! -e B"


def _cut(repo: Path, branch: str, files: dict[str, str]) -> str:
    """A candidate branch cut from HEAD with one commit; its tip."""
    return commit_on(repo, branch, files, message=f"add {', '.join(files)}")


def _resolver(repo: Path, script: str) -> RunSpec[Any]:
    """A resolver run that is a shell script: it commits, then reports."""
    agent = ShellAgent(f"set -e\n{script}\necho '{OK_OUTCOME_LINE}'")
    return Flow(repo, agent=agent, sandbox=NoSandbox()).run("resolve")


async def _land_all(queue: MergeQueue, count: int) -> list[Landing]:
    return [await anext(queue) for _ in range(count)]


@pytest.fixture
def base(host_repo: Path) -> str:
    """The commit every candidate is cut from; the target starts there too."""
    git(host_repo, "branch", TARGET)
    return git(host_repo, "rev-parse", "HEAD")


@pytest.mark.git
async def test_a_green_candidate_lands_exactly_the_commit_its_check_saw(
    host_repo: Path, base: str
) -> None:
    _cut(host_repo, "ticket-a", {"A": "a\n"})

    async with merge_queue(
        host_repo, TARGET, check=NOT_BOTH, sandbox=NoSandbox()
    ) as landings:
        landings.submit("ticket-a", base=base, name="a")
        (landing,) = await _land_all(landings, 1)

    assert isinstance(landing, Landed), landing
    assert landing.candidate.name == "a"
    assert landing.check is not None and landing.check.passed
    tip = git(host_repo, "rev-parse", TARGET)
    assert landing.check.revision == tip
    assert landing.report.target_before == base
    assert landing.report.target_after == tip
    assert subjects(host_repo, f"{base}..{TARGET}") == ["add A"]


@pytest.mark.git
async def test_two_candidates_green_alone_but_red_together_land_only_the_first(
    host_repo: Path, base: str
) -> None:
    _cut(host_repo, "ticket-a", {"A": "a\n"})
    b_tip = _cut(host_repo, "ticket-b", {"B": "b\n"})

    async with merge_queue(
        host_repo, TARGET, check=NOT_BOTH, sandbox=NoSandbox()
    ) as landings:
        landings.submit("ticket-a", base=base)
        landings.submit("ticket-b", base=base)
        landings.close()
        results = [landing async for landing in landings]

    first, second = results
    assert isinstance(first, Landed), first
    assert isinstance(second, CheckFailed), second
    assert not second.check.passed
    assert second.ref == "ticket-b"
    # The target stays at what was checked green; the candidate is untouched.
    assert git(host_repo, "rev-parse", TARGET) == first.report.target_after
    assert git(host_repo, "rev-parse", "ticket-b") == b_tip


@pytest.mark.git
async def test_a_failed_check_carries_what_the_check_wrote(
    host_repo: Path, base: str
) -> None:
    _cut(host_repo, "ticket-a", {"A": "a\n"})

    async with merge_queue(
        host_repo, TARGET, check="echo broke it >&2; exit 3", sandbox=NoSandbox()
    ) as landings:
        landings.submit("ticket-a", base=base)
        (landing,) = await _land_all(landings, 1)

    assert isinstance(landing, CheckFailed), landing
    assert landing.check.exit_code == 3
    assert "broke it" in landing.check.stderr
    assert git(host_repo, "rev-parse", TARGET) == base


@pytest.mark.git
async def test_candidates_land_in_the_order_they_arrive(
    host_repo: Path, base: str
) -> None:
    for name in ("C", "A", "B"):
        _cut(host_repo, f"ticket-{name}", {name: f"{name}\n"})

    async with merge_queue(
        host_repo, TARGET, check="true", sandbox=NoSandbox()
    ) as landings:
        for name in ("C", "A", "B"):
            landings.submit(f"ticket-{name}", base=base)
        landings.close()
        results = [landing async for landing in landings]

    assert all(isinstance(landing, Landed) for landing in results), results
    assert subjects(host_repo, f"{base}..{TARGET}") == ["add B", "add A", "add C"]


@pytest.mark.git
async def test_a_candidate_that_conflicts_on_the_new_head_is_evicted_untouched(
    host_repo: Path, base: str
) -> None:
    _cut(host_repo, "ticket-a", {"README": "one\n"})
    b_tip = _cut(host_repo, "ticket-b", {"README": "two\n"})

    async with merge_queue(
        host_repo, TARGET, check="true", sandbox=NoSandbox()
    ) as landings:
        landings.submit("ticket-a", base=base)
        landings.submit("ticket-b", base=base)
        first, second = await _land_all(landings, 2)

    assert isinstance(first, Landed), first
    assert isinstance(second, LandingConflicted), second
    assert second.conflict.paths == ("README",)
    assert second.head == first.report.target_after
    assert git(host_repo, "rev-parse", TARGET) == first.report.target_after
    assert git(host_repo, "rev-parse", "ticket-b") == b_tip


@pytest.mark.git
async def test_a_range_with_a_merge_in_it_is_evicted_as_refused(
    host_repo: Path, base: str
) -> None:
    _cut(host_repo, "side", {"S": "s\n"})
    _cut(host_repo, "ticket-a", {"A": "a\n"})
    git(host_repo, "checkout", "-q", "ticket-a")
    git(host_repo, "merge", "-q", "--no-ff", "-m", "update branch", "side")
    git(host_repo, "checkout", "-q", "-")

    async with merge_queue(
        host_repo, TARGET, check="true", sandbox=NoSandbox()
    ) as landings:
        landings.submit("ticket-a", base=base)
        (landing,) = await _land_all(landings, 1)

    assert isinstance(landing, LandingFailed), landing
    assert isinstance(landing.failure, Refused)
    assert landing.failure.reason == "nonlinear_series"
    assert git(host_repo, "rev-parse", TARGET) == base


@pytest.mark.git
async def test_a_resolver_at_the_front_resolves_a_conflict_against_the_head_it_lands_on(
    host_repo: Path, base: str
) -> None:
    _cut(host_repo, "ticket-a", {"README": "one\n"})
    _cut(host_repo, "ticket-b", {"README": "two\n"})
    attempts: list[Attempt] = []

    def resolve(attempt: Attempt) -> RunSpec[Any] | None:
        attempts.append(attempt)
        return _resolver(
            host_repo, "printf 'one\\ntwo\\n' > README\ngit commit -qam 'resolve'"
        )

    async with merge_queue(
        host_repo, TARGET, check="true", sandbox=NoSandbox(), resolve=resolve
    ) as landings:
        landings.submit("ticket-a", base=base)
        landings.submit("ticket-b", base=base)
        first, second = await _land_all(landings, 2)

    assert isinstance(first, Landed), first
    assert isinstance(second, Landed), second
    (attempt,) = attempts
    assert attempt.conflict is not None and attempt.check is None
    assert attempt.rounds == 0
    # The resolver starts at the head the candidate will land on.
    assert attempt.head == attempt.start == first.report.target_after
    (resolution,) = second.resolutions
    assert isinstance(resolution, RunSucceeded)
    assert resolution.base_sha == attempt.head
    assert git(host_repo, "show", f"{TARGET}:README") == "one\ntwo"


@pytest.mark.git
async def test_a_resolver_for_a_failed_check_starts_at_the_tree_that_failed(
    host_repo: Path, base: str
) -> None:
    _cut(host_repo, "ticket-a", {"A": "a\n"})
    _cut(host_repo, "ticket-b", {"B": "b\n"})
    attempts: list[Attempt] = []

    def resolve(attempt: Attempt) -> RunSpec[Any] | None:
        attempts.append(attempt)
        return _resolver(host_repo, "git rm -q A\ngit commit -qm 'drop A'")

    async with merge_queue(
        host_repo, TARGET, check=NOT_BOTH, sandbox=NoSandbox(), resolve=resolve
    ) as landings:
        landings.submit("ticket-a", base=base)
        landings.submit("ticket-b", base=base)
        first, second = await _land_all(landings, 2)

    assert isinstance(first, Landed), first
    assert isinstance(second, Landed), second
    (attempt,) = attempts
    assert attempt.check is not None and not attempt.check.passed
    assert attempt.start == attempt.check.revision != attempt.head
    assert subjects(host_repo, f"{first.report.target_after}..{TARGET}") == [
        "drop A",
        "add B",
    ]


@pytest.mark.git
async def test_a_resolver_that_gives_up_evicts_the_candidate_with_every_round(
    host_repo: Path, base: str, tmp_path: Path
) -> None:
    _cut(host_repo, "ticket-a", {"A": "a\n"})
    _cut(host_repo, "ticket-b", {"B": "b\n"})
    rounds: list[int] = []
    tally = tmp_path / "checks"
    counted = f"echo >> '{tally.as_posix()}'; {NOT_BOTH}"

    def resolve(attempt: Attempt) -> RunSpec[Any] | None:
        rounds.append(attempt.rounds)
        if attempt.rounds >= 2:
            return None
        return _resolver(host_repo, "true")  # changes nothing

    async with merge_queue(
        host_repo, TARGET, check=counted, sandbox=NoSandbox(), resolve=resolve
    ) as landings:
        landings.submit("ticket-a", base=base)
        landings.submit("ticket-b", base=base)
        _, second = await _land_all(landings, 2)

    assert isinstance(second, CheckFailed), second
    assert rounds == [0, 1, 2]
    assert len(second.resolutions) == 2
    # A round that brought nothing new is not checked again: one check for
    # ticket-a, one for ticket-b, however many rounds the resolver had.
    assert tally.read_text().count("\n") == 2


@pytest.mark.git
async def test_a_resolver_that_raises_evicts_the_candidate_as_a_value(
    host_repo: Path, base: str
) -> None:
    _cut(host_repo, "ticket-a", {"README": "one\n"})
    _cut(host_repo, "ticket-b", {"README": "two\n"})

    def resolve(attempt: Attempt) -> RunSpec[Any] | None:
        raise ValueError("no resolver today")

    async with merge_queue(
        host_repo, TARGET, check="true", sandbox=NoSandbox(), resolve=resolve
    ) as landings:
        landings.submit("ticket-a", base=base)
        landings.submit("ticket-b", base=base)
        _, second = await _land_all(landings, 2)

    assert isinstance(second, LandingFailed), second
    assert second.stage is None


@pytest.mark.git
async def test_a_target_moved_by_someone_else_mid_check_is_refused_not_overwritten(
    host_repo: Path, base: str
) -> None:
    _cut(host_repo, "ticket-a", {"A": "a\n"})
    gate = Gate()

    async with merge_queue(
        host_repo, TARGET, check="true", sandbox=GatedSandbox(gate, "start")
    ) as landings:
        landings.submit("ticket-a", base=base)
        await until_batch(gate.reached.is_set, landings)
        outside = commit_on(host_repo, TARGET, {"O": "o\n"}, message="outside")
        gate.release.set()
        (landing,) = await _land_all(landings, 1)

    assert isinstance(landing, LandingFailed), landing
    assert isinstance(landing.failure, Refused)
    assert landing.failure.reason == "target_moved"
    assert git(host_repo, "rev-parse", TARGET) == outside


def _stalls_on_a(marker: Path) -> str:
    """A check that, on a tree holding A, says so and then works until stopped."""
    return f"if test -e A; then touch '{marker.as_posix()}'; sleep 600; fi"


@pytest.mark.git
async def test_leaving_the_block_stops_the_check_and_lands_nothing(
    host_repo: Path, base: str, tmp_path: Path
) -> None:
    _cut(host_repo, "ticket-a", {"A": "a\n"})
    _cut(host_repo, "ticket-b", {"B": "b\n"})
    marker = tmp_path / "checking"

    async with merge_queue(
        host_repo, TARGET, check=_stalls_on_a(marker), sandbox=NoSandbox()
    ) as landings:
        landings.submit("ticket-a", base=base)
        landings.submit("ticket-b", base=base)
        await until_batch(marker.exists, landings)

    # Nothing landed, the check's workspace is gone, and ticket-b never began.
    assert git(host_repo, "rev-parse", TARGET) == base
    assert workspaces(tmp_path) == []


@pytest.mark.git
async def test_one_candidate_stopped_alone_lets_the_next_go_on(
    host_repo: Path, base: str, tmp_path: Path
) -> None:
    _cut(host_repo, "ticket-a", {"A": "a\n"})
    _cut(host_repo, "ticket-b", {"B": "b\n"})
    marker = tmp_path / "checking"

    async with merge_queue(
        host_repo, TARGET, check=_stalls_on_a(marker), sandbox=NoSandbox()
    ) as landings:
        first = landings.submit("ticket-a", base=base)
        landings.submit("ticket-b", base=base)
        await until_batch(marker.exists, landings)
        await first.cancel()
        landings.close()
        results = [landing async for landing in landings]

    assert first.candidate.ref == "ticket-a"
    (landing,) = results
    assert isinstance(landing, Landed), landing
    assert subjects(host_repo, f"{base}..{TARGET}") == ["add B"]


@pytest.mark.git
async def test_an_empty_range_lands_nothing_and_checks_nothing(
    host_repo: Path, base: str
) -> None:
    async with merge_queue(
        host_repo, TARGET, check="exit 1", sandbox=NoSandbox()
    ) as landings:
        landings.submit(TARGET, base=base)
        (landing,) = await _land_all(landings, 1)

    assert isinstance(landing, Landed), landing
    assert landing.check is None
    assert landing.report.landed == ()


@pytest.mark.git
async def test_a_closed_merge_queue_takes_no_more(host_repo: Path, base: str) -> None:
    async with merge_queue(
        host_repo, TARGET, check="true", sandbox=NoSandbox()
    ) as landings:
        landings.close()
        with pytest.raises(RuntimeError, match="takes no more"):
            landings.submit("ticket-a", base=base)


@pytest.mark.git
async def test_a_check_runs_against_the_bare_target_with_the_same_command(
    host_repo: Path, base: str
) -> None:
    # What a caller runs after a failed check, to tell "this candidate broke
    # it" from "the target was already red" — its policy, not the queue's.
    commit_on(host_repo, TARGET, {"A": "a\n", "B": "b\n"})

    red = await run_check(host_repo, TARGET, NOT_BOTH, NoSandbox())
    green = await run_check(host_repo, base, NOT_BOTH, NoSandbox())

    assert not red.passed
    assert red.revision == git(host_repo, "rev-parse", TARGET)
    assert green.passed
    assert green.revision == base


@pytest.mark.git
async def test_a_check_given_as_argv_runs_without_a_shell(host_repo: Path) -> None:
    checked = await run_check(host_repo, "HEAD", ["git", "status"], NoSandbox())
    assert checked.passed


@pytest.mark.unit
def test_an_attempt_holds_a_conflict_or_a_failed_check_never_both_or_neither() -> None:
    candidate = Candidate(ref="ticket-a", base="0" * 40)
    failed = CheckResult(revision="1" * 40, exit_code=1, stdout="", stderr="")
    with pytest.raises(ValueError, match="never both or neither"):
        Attempt(candidate, "ticket-a", "2" * 40, "2" * 40)
    with pytest.raises(ValueError, match="never both or neither"):
        Attempt(
            candidate, "ticket-a", "2" * 40, "2" * 40, Conflict(paths=("A",)), failed
        )


class _Urgent:
    """Ranks the candidate named ``urgent`` first, and the rest as they came."""

    def pick(self, waiting: Sequence[QueuedCandidate]) -> QueuedCandidate:
        return next((q for q in waiting if q.candidate.name == "urgent"), waiting[0])


@pytest.mark.git
async def test_an_ordering_strategy_says_which_candidate_reaches_the_front(
    host_repo: Path, base: str
) -> None:
    # The same protocol a run queue takes (ADR-0048), over candidates.
    for name in ("C", "A", "B"):
        _cut(host_repo, f"ticket-{name}", {name: f"{name}\n"})

    async with merge_queue(
        host_repo, TARGET, check="true", sandbox=NoSandbox(), order=_Urgent()
    ) as landings:
        landings.submit("ticket-C", base=base)
        landings.submit("ticket-A", base=base, name="urgent")
        landings.submit("ticket-B", base=base)
        landings.close()
        results = [landing async for landing in landings]

    assert all(isinstance(landing, Landed) for landing in results), results
    assert subjects(host_repo, f"{base}..{TARGET}") == ["add B", "add C", "add A"]


class _Broken:
    def pick(self, waiting: Sequence[QueuedCandidate]) -> QueuedCandidate:
        raise LookupError("no ranking today")


@pytest.mark.git
async def test_a_failing_ordering_strategy_refuses_its_candidates_as_values(
    host_repo: Path, base: str
) -> None:
    _cut(host_repo, "ticket-a", {"A": "a\n"})
    _cut(host_repo, "ticket-b", {"B": "b\n"})

    async with merge_queue(
        host_repo, TARGET, check="true", sandbox=NoSandbox(), order=_Broken()
    ) as landings:
        landings.submit("ticket-a", base=base)
        landings.submit("ticket-b", base=base)
        landings.close()
        results = [landing async for landing in landings]

    assert len(results) == 2
    for landing in results:
        assert isinstance(landing, LandingFailed), landing
        assert landing.stage is None
        assert isinstance(landing.failure, Errored)
    assert git(host_repo, "rev-parse", TARGET) == base
