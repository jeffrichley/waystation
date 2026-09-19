"""DockerSandbox's argv plans, checked without Docker (ADR-0010).

These are the only Docker coverage on Windows CI, so they pin what each plan
says: labels, user, transport, the environment allowlist and ``run_args``.
"""

from __future__ import annotations

from itertools import pairwise

import pytest

from waystation import DockerSandbox
from waystation.sandbox._docker_plans import (
    Transport,
    plan_create,
    plan_destroy,
    plan_exec,
    plan_labelled,
    resolve_transport,
)

IMAGE = "flow-image:1"


def _create(
    *,
    env: dict[str, str] | None = None,
    bind_source: str | None = None,
    run_args: tuple[str, ...] = (),
) -> tuple[str, ...]:
    return plan_create(
        image=IMAGE,
        name="waystation-1a2b3c4d-000000",
        run_id="1a2b3c4d",
        env=env or {},
        bind_source=bind_source,
        run_args=run_args,
    )


def _flags_before_image(argv: tuple[str, ...]) -> tuple[str, ...]:
    return argv[: argv.index(IMAGE)]


def _env_flags(argv: tuple[str, ...]) -> list[str]:
    return [argv[i + 1] for i, arg in enumerate(argv) if arg in ("-e", "--env")]


@pytest.mark.unit
def test_a_sandbox_is_labelled_with_its_run_id() -> None:
    argv = _create()

    assert ("--label", "waystation.run-id=1a2b3c4d") in pairwise(argv)


@pytest.mark.unit
def test_a_sandbox_is_named_so_teardown_can_find_it_before_run_returns() -> None:
    argv = _create()

    assert ("--name", "waystation-1a2b3c4d-000000") in pairwise(argv)


@pytest.mark.unit
def test_a_sandbox_idles_detached_until_work_is_execd_into_it() -> None:
    argv = _create()

    assert argv[:3] == ("docker", "run", "-d")
    assert ("--entrypoint", "sleep") in pairwise(argv)
    assert argv[-2:] == (IMAGE, "infinity")


@pytest.mark.unit
def test_a_sandbox_runs_as_its_images_own_user() -> None:
    # The image's USER applies: no plan ever names another one.
    for argv in (_create(), plan_exec("c", ["id"], env={}, stdin=False)):
        assert not {"-u", "--user"} & set(argv)
        assert not any(arg.startswith("--user=") for arg in argv)


@pytest.mark.unit
def test_a_sandbox_never_pulls_its_image() -> None:
    # A missing image fails the start instead of being fetched (ADR-0011).
    assert "--pull=never" in _flags_before_image(_create())


@pytest.mark.unit
def test_only_the_allowlisted_environment_reaches_the_sandbox() -> None:
    argv = _create(env={"TOKEN": "s3cret", "GREETING": "two words"})

    assert _env_flags(argv) == ["TOKEN=s3cret", "GREETING=two words"]
    assert not any(arg.startswith("--env-file") for arg in argv)


@pytest.mark.unit
def test_an_empty_allowlist_renders_no_environment() -> None:
    assert _env_flags(_create()) == []


@pytest.mark.unit
def test_run_args_follow_waystations_options_and_precede_the_image() -> None:
    # Last flag wins in the docker CLI, so the escape hatch can override ours.
    argv = _create(run_args=("--network", "none", "--workdir", "/elsewhere"))

    before = _flags_before_image(argv)
    assert before[-4:] == ("--network", "none", "--workdir", "/elsewhere")
    assert before.index("--workdir") < before.index("--network")


@pytest.mark.unit
def test_bind_mounts_the_host_workspace_at_the_workspace_root() -> None:
    argv = _create(bind_source="/tmp/waystation-1a2b3c4d-x")

    assert (
        "--mount",
        "type=bind,source=/tmp/waystation-1a2b3c4d-x,target=/workspace",
    ) in pairwise(argv)


@pytest.mark.unit
def test_copy_mounts_nothing() -> None:
    assert "--mount" not in _create()


@pytest.mark.unit
def test_a_sandbox_starts_in_the_workspace_root() -> None:
    argv = _create()

    assert ("--workdir", "/workspace") in pairwise(argv)


@pytest.mark.unit
def test_every_exec_runs_in_the_workspace_root_of_its_container() -> None:
    argv = plan_exec("box", ["git", "status"], env={}, stdin=False)

    assert argv[:2] == ("docker", "exec")
    assert ("--workdir", "/workspace") in pairwise(argv)
    assert argv[-3:] == ("box", "git", "status")


@pytest.mark.unit
def test_an_exec_keeps_stdin_open_only_when_it_is_given_some() -> None:
    assert "-i" in plan_exec("box", ["cat"], env={}, stdin=True)
    assert "-i" not in plan_exec("box", ["cat"], env={}, stdin=False)


@pytest.mark.unit
def test_an_execs_own_environment_is_rendered_for_that_exec() -> None:
    argv = plan_exec("box", ["env"], env={"GIT_INDEX_FILE": "x"}, stdin=False)

    assert _env_flags(argv) == ["GIT_INDEX_FILE=x"]
    assert argv.index("box") > argv.index("-e")


@pytest.mark.unit
def test_teardown_forces_removal_and_never_stops() -> None:
    # stop burns a 10 s grace on an idle PID 1; rm -f does not (ADR-0014).
    # -v takes anonymous volumes an image declares along with the container.
    assert plan_destroy("box") == ("docker", "rm", "-f", "-v", "box")


@pytest.mark.unit
def test_teardown_can_remove_many_sandboxes_in_one_call() -> None:
    assert plan_destroy("a", "b") == ("docker", "rm", "-f", "-v", "a", "b")


@pytest.mark.unit
def test_reaping_a_run_finds_its_sandboxes_by_label_running_or_not() -> None:
    argv = plan_labelled("1a2b3c4d")

    assert argv[:2] == ("docker", "ps")
    assert {"--all", "--quiet"} <= set(argv)
    assert ("--filter", "label=waystation.run-id=1a2b3c4d") in pairwise(argv)


@pytest.mark.unit
def test_reaping_every_run_finds_every_labelled_sandbox() -> None:
    argv = plan_labelled(None)

    assert ("--filter", "label=waystation.run-id") in pairwise(argv)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("platform", "transport"),
    [("win32", "copy"), ("linux", "bind"), ("darwin", "bind")],
)
def test_auto_copies_on_windows_and_binds_elsewhere(
    platform: str, transport: str
) -> None:
    assert resolve_transport("auto", platform=platform) == transport


@pytest.mark.unit
@pytest.mark.parametrize("chosen", ["copy", "bind"])
def test_a_chosen_transport_is_kept_on_every_host(chosen: Transport) -> None:
    for platform in ("win32", "linux", "darwin"):
        assert resolve_transport(chosen, platform=platform) == chosen


@pytest.mark.unit
def test_equal_specs_compare_equal() -> None:
    # Fan-out preflights each distinct spec once; a list and a tuple of the
    # same run_args describe the same sandbox.
    a = DockerSandbox(IMAGE, env={"A": "1"}, pass_env=["TOKEN"], run_args=["--x"])
    b = DockerSandbox(IMAGE, env={"A": "1"}, pass_env=("TOKEN",), run_args=("--x",))

    assert a == b
    assert a != DockerSandbox(IMAGE, transport="copy")


@pytest.mark.unit
def test_a_spec_is_a_value_its_callers_dicts_cannot_change() -> None:
    env = {"A": "1"}
    spec = DockerSandbox(IMAGE, env=env)
    env["A"] = "2"

    assert spec.env == {"A": "1"}


@pytest.mark.unit
def test_an_unknown_transport_is_refused_when_the_spec_is_made() -> None:
    with pytest.raises(ValueError, match="transport"):
        DockerSandbox(IMAGE, transport="rsync")  # type: ignore[arg-type]
