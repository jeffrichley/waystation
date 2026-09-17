"""Failure union and error-family shape (issue #21)."""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from waystation import (
    AgentExited,
    CommandFailed,
    Errored,
    Failure,
    HookRaised,
    OutcomeInvalid,
    OutcomeMissing,
    PreflightError,
    Refused,
    RunFailed,
    StageError,
    TimedOut,
    WaystationError,
)


def _validation_error() -> ValidationError:
    try:
        TypeAdapter(dict[str, int]).validate_python({"x": "no"})
    except ValidationError as exc:
        return exc
    msg = "expected ValidationError"
    raise AssertionError(msg)


@pytest.mark.unit
def test_failure_union_members_are_matchable() -> None:
    failures: list[Failure] = [
        TimedOut(bound="agent_wall", limit=1.0, elapsed=1.5),
        AgentExited(exit_code=1, stdout_tail="", stderr_tail="", outcome=None),
        OutcomeMissing(stdout_tail=""),
        OutcomeInvalid(raw={"x": "no"}, error=_validation_error()),
        HookRaised(
            hook="agent_end",
            function="user_hook",
            exception=RuntimeError("h"),
        ),
        CommandFailed(argv=("git", "status"), exit_code=1, stderr_tail="err"),
        Refused(reason="dirty_tree", detail="tracked changes"),
        Errored(exception=RuntimeError("boom")),
    ]

    kinds = []
    for failure in failures:
        match failure:
            case TimedOut():
                kinds.append("TimedOut")
            case AgentExited():
                kinds.append("AgentExited")
            case OutcomeMissing():
                kinds.append("OutcomeMissing")
            case OutcomeInvalid():
                kinds.append("OutcomeInvalid")
            case HookRaised():
                kinds.append("HookRaised")
            case CommandFailed():
                kinds.append("CommandFailed")
            case Refused():
                kinds.append("Refused")
            case Errored():
                kinds.append("Errored")
    assert kinds == [
        "TimedOut",
        "AgentExited",
        "OutcomeMissing",
        "OutcomeInvalid",
        "HookRaised",
        "CommandFailed",
        "Refused",
        "Errored",
    ]


@pytest.mark.unit
def test_waystation_error_family() -> None:
    assert issubclass(PreflightError, WaystationError)
    assert issubclass(StageError, WaystationError)
    err = StageError("agent", OutcomeMissing(stdout_tail=""))
    assert err.stage == "agent"
    assert isinstance(err.failure, OutcomeMissing)


@pytest.mark.unit
def test_run_failed_carries_stage_and_failure() -> None:
    result = RunFailed(
        run_id="abcd1234",
        name=None,
        base_sha=None,
        elapsed={},
        agent=None,
        series=None,
        preserved=None,
        stage="agent",
        failure=OutcomeMissing(stdout_tail="tail"),
    )
    match result:
        case RunFailed(stage="agent", failure=OutcomeMissing(stdout_tail="tail")):
            pass
        case _:
            pytest.fail("RunFailed did not match")
